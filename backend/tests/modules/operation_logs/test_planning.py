from datetime import UTC, date, datetime

from sqlmodel import select

from app.modules.operation_logs.models import OperationSyncCursor
from app.modules.operation_logs.planning import plan_logs
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc, ReportSyncJob
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed
from tests.modules.reporting.test_jobs import _route


def test_recent_and_history_have_separate_cursors_and_shared_account_owner(
    session, sync_seed
):
    seed = sync_seed
    session.add(
        AdSyncAccount(
            tenant_id=seed.tenant_id, advertiser_id="acc-1", owner_bc_id="bc-a"
        )
    )
    session.add(
        AdSyncAccount(
            tenant_id=seed.tenant_id, advertiser_id="acc-2", owner_bc_id="bc-a"
        )
    )
    route = _route(seed)
    plan = AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-a",
        actor_id=seed.actor_id,
        frozen_route=route,
    )
    other = AdSyncBc(
        tenant_id=seed.tenant_id,
        bc_id="bc-b",
        actor_id=seed.actor_id,
        frozen_route={**route, "bc_id": "bc-b"},
    )
    session.flush()
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    assert plan_logs(session, plan, now=now, history_start=date(2026, 8, 1)) == 4
    assert plan_logs(session, other, now=now, history_start=date(2026, 8, 1)) == 0
    rows = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
    ).all()
    assert all(r.kind == "operation_log" and len(r.advertiser_ids) == 1 for r in rows)
    assert all((r.end_date - r.start_date).days < 29 for r in rows)
    assert sorted({r.priority for r in rows}) == [3, 4]
    assert plan_logs(session, plan, now=now, history_start=date(2026, 8, 1)) == 2
    assert session.get(
        OperationSyncCursor, (seed.tenant_id, "acc-1")
    ).recent_day == date(2026, 11, 10)

    assert plan_logs(session, plan, now=now, history_start=date(2026, 7, 1)) == 2
    assert any(
        r.start_date == date(2026, 7, 1)
        for r in session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
    )
