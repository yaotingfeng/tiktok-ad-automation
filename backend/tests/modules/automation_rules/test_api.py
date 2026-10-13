from uuid import uuid4

from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_definition import base


def url(context, suffix=""):
    return f"/api/tenants/{context.tenant_id}/automation-rules{suffix}"


def create(client, context, name="低效止损", **patch):
    return client.post(
        url(context),
        headers=headers(context),
        json={"name": name, "definition": base(**patch)},
    )


def test_crud_and_versioning(client, context):
    created = create(client, context)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["version"] == 1 and rule["next_run_at"]
    listed = client.get(url(context), headers=headers(context)).json()
    assert listed["total"] == 1
    update = {
        "name": "改名",
        "definition": base(),
        "enabled": True,
        "expected_version": 1,
    }
    assert (
        client.put(
            url(context, f"/{rule['id']}"), headers=headers(context), json=update
        ).json()["version"]
        == 2
    )
    stale = client.put(
        url(context, f"/{rule['id']}"), headers=headers(context), json=update
    )
    assert stale.status_code == 409
    off = client.post(
        url(context, f"/{rule['id']}/enabled"),
        headers=headers(context),
        json={"enabled": False},
    )
    assert off.json()["enabled"] is False and off.json()["next_run_at"] is None
    assert (
        client.delete(
            url(context, f"/{rule['id']}"), headers=headers(context)
        ).status_code
        == 204
    )


def test_rejects_non_notify_mode_and_duplicates(client, context):
    assert create(client, context, mode="AUTO").status_code == 409
    assert create(client, context).status_code == 201
    assert create(client, context).status_code == 409


def test_invalid_definition_422(client, context):
    assert create(client, context, conditions=[]).status_code == 422


def test_viewer_reads_but_cannot_write(client, viewer_context):
    assert create(client, viewer_context).status_code == 403
    assert (
        client.get(url(viewer_context), headers=headers(viewer_context)).status_code
        == 200
    )


def test_tenant_isolation(client, context, other_context):
    rule = create(client, context).json()
    assert (
        client.get(
            url(other_context, f"/{rule['id']}"), headers=headers(other_context)
        ).status_code
        == 404
    )
    assert (
        client.get(url(context, f"/{uuid4()}"), headers=headers(context)).status_code
        == 404
    )


def test_catalog_lists_metrics_with_official_names(client, context):
    body = client.get(url(context, "/catalog"), headers=headers(context)).json()
    spend = next(m for m in body["metrics"] if m["key"] == "spend")
    assert spend["name_zh"] == "消耗" and "material" in spend["levels"]
    assert body["action_levels"]["BUDGET"] == ["campaign", "adgroup"]


def test_dry_run_service_hits_without_persisting(session, sync_seed):
    from sqlmodel import select

    from app.modules.automation_rules.models import AutomationRuleRun
    from app.modules.automation_rules.schemas import RuleDefinition
    from app.modules.automation_rules.service import dry_run
    from tests.modules.automation_rules.test_inputs import NOW, _seed

    _seed(session, sync_seed)
    result = dry_run(
        session,
        context=sync_seed.context,
        now=NOW,
        definition=RuleDefinition.model_validate(
            base(
                conditions=[
                    {
                        "metric": "spend",
                        "range": "PAST_3_DAYS",
                        "operator": "GT",
                        "values": ["3"],
                    }
                ]
            )
        ),
    )
    # g1 近 3 天 14、g9 只有 2,门槛 3 只命中 g1。
    assert [h.subject_key for h in result.hits] == ["adgroup:acc-1:g1"]
    assert session.exec(select(AutomationRuleRun)).all() == []


def test_dry_run_http_reports_stale_accounts(client, session, sync_seed):
    from sqlalchemy import update

    from app.modules.reporting.store_models import ReportSyncState
    from tests.modules.automation_rules.test_inputs import _seed

    _seed(session, sync_seed)
    # 接口使用真实当前时间;清空同步成功时间,使两个账户在任何时刻都判为数据过期。
    session.exec(update(ReportSyncState).values(last_success_at=None))
    ctx = sync_seed.context
    response = client.post(
        url(ctx, "/dry-run"), headers=headers(ctx), json={"definition": base()}
    )
    assert response.status_code == 200, response.text
    assert {a["advertiser_id"] for a in response.json()["skipped_accounts"]} == {
        "acc-1",
        "acc-2",
    }
