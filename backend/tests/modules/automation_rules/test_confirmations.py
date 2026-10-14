import pytest
from sqlalchemy import update
from sqlmodel import select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.automation_rules.confirmations import (
    confirm,
    confirmation_detail,
    dismiss,
    list_confirmations,
)
from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRuleHit,
)
from app.modules.automation_rules.runner import execute_run
from app.modules.reporting.store_models import AdReportDay
from app.modules.tenants.models import TenantMembership
from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_inputs import NOW, _seed
from tests.modules.automation_rules.test_runner import make_rule


@pytest.fixture
def batch(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(
        session,
        sync_seed,
        mode="CONFIRM",
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]}
        ],
    )
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    return session.exec(
        select(AutomationConfirmation).where(AutomationConfirmation.run_id == run.id)
    ).one()


def test_list_shows_pending_batches(session, sync_seed, batch):
    page = list_confirmations(session, context=sync_seed.context, status="PENDING")
    assert [item.id for item in page.items] == [batch.id]
    assert page.items[0].hit_count == 2


def test_confirm_detail_rejudges_with_current_data(session, sync_seed, batch):
    # 当前数据下 b1 的消耗降到 0，不再满足「消耗 > 1」。
    session.exec(update(AdReportDay).where(AdReportDay.ad_id == "b1").values(spend=0))
    detail = confirmation_detail(
        session, context=sync_seed.context, confirmation_id=batch.id, now=NOW
    )
    matching = {item.subject_key: item.still_matching for item in detail.items}
    assert matching == {"adgroup:acc-1:g1": True, "adgroup:acc-2:g9": False}
    then = {item.subject_key: item.metrics_then for item in detail.items}
    assert then["adgroup:acc-2:g9"]["spend:TODAY"] == "2"


def test_confirm_queues_execution_with_selected_keys(session, sync_seed, batch):
    result = confirm(
        session,
        context=sync_seed.context,
        confirmation_id=batch.id,
        subject_keys=["adgroup:acc-1:g1"],
        now=NOW,
    )
    assert result.status == "CONFIRMED"
    queued = session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == "automation.execute")
    ).all()
    assert [d.payload for d in queued] == [
        {
            "run_id": str(batch.run_id),
            "mode": "CONFIRM",
            "selected_keys": ["adgroup:acc-1:g1"],
        }
    ]


def test_confirm_twice_is_conflict(session, sync_seed, batch):
    confirm(
        session,
        context=sync_seed.context,
        confirmation_id=batch.id,
        subject_keys=["adgroup:acc-1:g1"],
        now=NOW,
    )
    with pytest.raises(DomainError) as error:
        confirm(
            session,
            context=sync_seed.context,
            confirmation_id=batch.id,
            subject_keys=["adgroup:acc-1:g1"],
            now=NOW,
        )
    assert error.value.code == "automation_confirmation_closed"


def test_confirm_rejects_unknown_or_empty_keys(session, sync_seed, batch):
    for keys in ([], ["adgroup:acc-9:x"]):
        with pytest.raises(DomainError):
            confirm(
                session,
                context=sync_seed.context,
                confirmation_id=batch.id,
                subject_keys=keys,
                now=NOW,
            )


def test_dismiss_closes_batch(session, sync_seed, batch):
    dismiss(session, context=sync_seed.context, confirmation_id=batch.id, now=NOW)
    assert batch.status == "DISMISSED"
    outcomes = {
        h.outcome
        for h in session.exec(
            select(AutomationRuleHit).where(AutomationRuleHit.run_id == batch.run_id)
        ).all()
    }
    assert outcomes == {"NOT_SELECTED"}


def test_viewer_cannot_confirm(session, sync_seed, batch):
    session.get(
        TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)
    ).role = "viewer"
    session.flush()
    with pytest.raises(DomainError):
        confirm(
            session,
            context=sync_seed.context,
            confirmation_id=batch.id,
            subject_keys=["adgroup:acc-1:g1"],
            now=NOW,
        )


def test_other_tenant_cannot_see_batch(client, other_context, batch):
    url = f"/api/tenants/{other_context.tenant_id}/automation-confirmations/{batch.id}"
    assert client.get(url, headers=headers(other_context)).status_code == 404


def test_http_confirm_flow(client, sync_seed, batch):
    ctx = sync_seed.context
    base_url = f"/api/tenants/{ctx.tenant_id}/automation-confirmations"
    assert client.get(base_url, headers=headers(ctx)).json()["items"][0]["id"] == str(
        batch.id
    )
    response = client.post(
        f"{base_url}/{batch.id}/confirm",
        headers=headers(ctx),
        json={"subject_keys": ["adgroup:acc-1:g1"]},
    )
    assert response.status_code == 200, response.text
    again = client.post(f"{base_url}/{batch.id}/dismiss", headers=headers(ctx))
    assert again.status_code == 409
