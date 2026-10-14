"""固定时间汇总发送：到点合并该时段的运行结果为一张卡片。"""

from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import AutomationRuleRun
from app.modules.automation_rules.runner import execute_run
from app.modules.automation_rules.schemas import (
    AutomationSettingsUpdate,
    NotificationSetting,
)
from app.modules.automation_rules.settings_service import update_settings
from app.modules.automation_rules.summary import (
    deliver_summary,
    due_slot,
    scan_summaries,
)
from tests.modules.automation_rules.test_inputs import NOW, _seed
from tests.modules.automation_rules.test_runner import HOOK, make_rule

SUMMARY = {"enabled": True, "timing": "SUMMARY", "summary_times": ["09:00", "18:00"]}


def test_notification_setting_validation():
    setting = NotificationSetting.model_validate(
        {"timing": "SUMMARY", "summary_times": ["18:00", "09:00", "09:00"]}
    )
    assert setting.summary_times == (time(9), time(18))
    with pytest.raises(ValidationError):
        NotificationSetting.model_validate({"timing": "SUMMARY", "summary_times": []})
    with pytest.raises(ValidationError):
        NotificationSetting.model_validate(
            {"timing": "EVERY_RUN", "summary_times": ["09:00"]}
        )
    with pytest.raises(ValidationError):
        NotificationSetting.model_validate(
            {"timing": "SUMMARY", "summary_times": [f"0{i}:00" for i in range(7)]}
        )


def test_due_slot_in_tenant_timezone():
    times = (time(9), time(18))
    # 上海 2026-10-14 11:00 → 最近一个汇总时刻是当天 09:00（UTC 01:00）。
    now = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)
    assert due_slot(times, "Asia/Shanghai", now) == datetime(
        2026, 10, 14, 1, 0, tzinfo=UTC
    )
    # 上海 08:00 → 前一天 18:00。
    early = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    assert due_slot(times, "Asia/Shanghai", early) == datetime(
        2026, 10, 13, 10, 0, tzinfo=UTC
    )


def test_due_slot_across_dst():
    # 纽约 2026-11-01 夏令时结束：当天 09:00 是 EST，即 UTC 14:00。
    now = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    assert due_slot((time(9),), "America/New_York", now) == datetime(
        2026, 11, 1, 14, 0, tzinfo=UTC
    )
    before = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
    # 前一天 09:00 是 EDT，即 UTC 13:00。
    assert due_slot((time(9),), "America/New_York", before) == datetime(
        2026, 10, 31, 13, 0, tzinfo=UTC
    )


def _summary_rule(session, seed, **patch):
    from app.modules.tenants.models import TenantMembership

    session.get(TenantMembership, (seed.tenant_id, seed.actor_id)).role = "tenant_admin"
    update_settings(
        session,
        context=seed.context,
        body=AutomationSettingsUpdate(
            schedule_timezone="Asia/Shanghai",
            paused=False,
            feishu_enabled=True,
            webhook_url=HOOK,
        ),
    )
    return make_rule(session, seed, notification=SUMMARY, **patch)


def _dispatches(session, name):
    return session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == name)
    ).all()


def test_summary_mode_defers_warning_cards(session, sync_seed):
    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.hit_count == 1 and run.notify_status == "SUMMARY"
    assert not _dispatches(session, "automation.send_feishu")


def test_confirmation_card_still_sent_immediately(session, sync_seed):
    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed, mode="CONFIRM")
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    # 待确认批次在下次运行即失效，延迟到汇总发送会失去确认的意义。
    assert run.notify_status == "PENDING"
    assert len(_dispatches(session, "automation.send_feishu")) == 1


def test_scan_records_first_slot_then_sends_once(session, sync_seed, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed)
    first = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)  # 上海 08:30
    scan_summaries(session, now=first)
    # 首次只记录最近的汇总时刻，不补发历史。
    assert rule.last_summary_at == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    assert not _dispatches(session, "automation.send_summary")
    at_nine = datetime(2026, 10, 14, 1, 1, tzinfo=UTC)
    scan_summaries(session, now=at_nine)
    scan_summaries(session, now=at_nine + timedelta(minutes=1))
    queued = _dispatches(session, "automation.send_summary")
    assert [d.payload for d in queued] == [
        {"rule_id": str(rule.id), "slot": "2026-10-14T01:00:00+00:00"}
    ]
    assert rule.last_summary_at == datetime(2026, 10, 14, 1, 0, tzinfo=UTC)


def test_deliver_summary_merges_runs(session, sync_seed):
    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed)
    first = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    later = NOW + timedelta(minutes=30)
    second = execute_run(session, rule=rule, slot_at=later, now=later)
    sent = []

    def handler(request):
        sent.append(request.read().decode())
        return httpx.Response(200, json={"code": 0})

    status = deliver_summary(
        session,
        rule_id=rule.id,
        slot=later + timedelta(minutes=5),
        transport=httpx.MockTransport(handler),
    )
    assert status == "SENT" and len(sent) == 1
    assert "【汇总】r" in sent[0] and "运行 2 次" in sent[0]
    assert first.notify_status == "SENT" and second.notify_status == "SENT"
    # 同一对象在多次运行中命中只列一次。
    assert sent[0].count("组一") == 1
    assert (
        deliver_summary(session, rule_id=rule.id, slot=later + timedelta(hours=1))
        == "NOT_REQUIRED"
    )


def test_summary_runs_after_slot_wait_for_next(session, sync_seed):
    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert (
        deliver_summary(session, rule_id=rule.id, slot=NOW - timedelta(minutes=1))
        == "NOT_REQUIRED"
    )
    assert session.get(AutomationRuleRun, run.id).notify_status == "SUMMARY"


def test_scan_respects_flag_and_disabled_rules(session, sync_seed, monkeypatch):
    from app.core.config import settings

    _seed(session, sync_seed)
    rule = _summary_rule(session, sync_seed)
    rule.last_summary_at = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    at_nine = datetime(2026, 10, 14, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", False)
    assert scan_summaries(session, now=at_nine) == 0
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    rule.enabled = False
    session.flush()
    assert scan_summaries(session, now=at_nine) == 0
    rule.enabled = True
    session.flush()
    assert scan_summaries(session, now=at_nine) == 1
