# test_settings.py
import httpx

from tests.modules.automation_rules.conftest import headers

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/token-9999"


def url(context):
    return f"/api/tenants/{context.tenant_id}/automation-settings"


def test_defaults(client, admin_context):
    body = client.get(url(admin_context), headers=headers(admin_context)).json()
    assert body == {
        "schedule_timezone": "Asia/Shanghai",
        "paused": False,
        "reverse_cooldown_hours": 24,
        "daily_increase_limit_pct": 100,
        "feishu_configured": False,
        "feishu_enabled": False,
        "feishu_url_hint": None,
        "feishu_has_secret": False,
    }


def test_settings_never_echo_secrets(client, admin_context):
    response = client.put(
        url(admin_context),
        headers=headers(admin_context),
        json={
            "schedule_timezone": "Asia/Shanghai",
            "paused": False,
            "feishu_enabled": True,
            "webhook_url": HOOK,
            "secret": "top-secret",
        },
    )
    assert response.status_code == 200, response.text
    for text in (
        response.text,
        client.get(url(admin_context), headers=headers(admin_context)).text,
    ):
        assert "token-9999" not in text and "top-secret" not in text
    assert response.json()["feishu_has_secret"] is True


def test_keep_webhook_when_omitted(client, admin_context, session):
    from app.modules.automation_rules.settings_service import load_feishu

    client.put(
        url(admin_context),
        headers=headers(admin_context),
        json={
            "schedule_timezone": "Asia/Shanghai",
            "paused": False,
            "feishu_enabled": True,
            "webhook_url": HOOK,
        },
    )
    client.put(
        url(admin_context),
        headers=headers(admin_context),
        json={
            "schedule_timezone": "Asia/Hong_Kong",
            "paused": True,
            "feishu_enabled": True,
        },
    )
    assert load_feishu(session, admin_context.tenant_id) == (HOOK, "")


def test_invalid_timezone_and_webhook(client, admin_context):
    for patch in (
        {"schedule_timezone": "Mars/Base"},
        {"webhook_url": "https://evil.com/x"},
    ):
        body = {
            "schedule_timezone": "Asia/Shanghai",
            "paused": False,
            "feishu_enabled": True,
            **patch,
        }
        assert (
            client.put(
                url(admin_context), headers=headers(admin_context), json=body
            ).status_code
            == 422
        )


def test_viewer_cannot_write(client, viewer_context):
    body = {
        "schedule_timezone": "Asia/Shanghai",
        "paused": False,
        "feishu_enabled": False,
    }
    assert (
        client.put(
            url(viewer_context), headers=headers(viewer_context), json=body
        ).status_code
        == 403
    )


def test_other_tenant_isolated(client, admin_context, session):
    from tests.modules.conftest import create_context

    other = create_context(session, role="tenant_admin")
    response = client.get(url(admin_context), headers=headers(other))
    assert response.status_code == 403


def test_send_test_uses_given_webhook(session, admin_context):
    from app.modules.automation_rules.schemas import FeishuTestRequest
    from app.modules.automation_rules.settings_service import send_test

    calls = []
    transport = httpx.MockTransport(
        lambda r: calls.append(r) or httpx.Response(200, json={"code": 0})
    )
    send_test(
        session,
        context=admin_context,
        body=FeishuTestRequest(webhook_url=HOOK),
        transport=transport,
    )
    assert len(calls) == 1


def test_execution_settings_roundtrip(client, admin_context):
    body = {
        "schedule_timezone": "Asia/Shanghai",
        "paused": False,
        "feishu_enabled": False,
        "reverse_cooldown_hours": 12,
        "daily_increase_limit_pct": 50,
    }
    assert (
        client.put(
            url(admin_context), headers=headers(admin_context), json=body
        ).status_code
        == 200
    )
    got = client.get(url(admin_context), headers=headers(admin_context)).json()
    assert got["reverse_cooldown_hours"] == 12 and got["daily_increase_limit_pct"] == 50
    body["reverse_cooldown_hours"] = 0
    assert (
        client.put(
            url(admin_context), headers=headers(admin_context), json=body
        ).status_code
        == 422
    )


def test_timezone_change_reschedules_enabled_rules(session, sync_seed):
    from datetime import UTC, datetime, time
    from zoneinfo import ZoneInfo

    from app.modules.automation_rules.models import AutomationRule
    from app.modules.automation_rules.schedule import next_run_at
    from app.modules.automation_rules.schemas import (
        AutomationSettingsUpdate,
        Schedule,
    )
    from app.modules.automation_rules.settings_service import update_settings
    from app.modules.tenants.models import TenantMembership
    from tests.modules.automation_rules.test_definition import base

    session.get(
        TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)
    ).role = "tenant_admin"
    daily = {"frequency": "DAILY", "daily_at": "09:00", "window": "CONTINUOUS"}
    rule = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="daily",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=base(schedule=daily),
        next_run_at=datetime(2026, 10, 15, 1, 0, tzinfo=UTC),  # 上海 09:00
    )
    session.add(rule)
    session.flush()
    update_settings(
        session,
        context=sync_seed.context,
        body=AutomationSettingsUpdate(
            schedule_timezone="America/New_York", paused=False, feishu_enabled=False
        ),
    )
    expected = next_run_at(
        Schedule.model_validate(daily),
        after=datetime.now(UTC),
        timezone="America/New_York",
    )
    assert rule.next_run_at == expected
    assert rule.next_run_at.astimezone(ZoneInfo("America/New_York")).time() == time(9)
