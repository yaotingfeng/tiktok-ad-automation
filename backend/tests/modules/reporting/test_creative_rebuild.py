"""历史回补只覆盖当前拥有账户的既有广告总账日期，幂等成对排队。"""

from datetime import UTC, datetime

from sqlmodel import select

from app.modules.reporting import publish
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import _route


def test_backfill_pairs_sources_and_is_scoped_idempotent(session, sync_seed):
    from app.modules.reporting.creative_rebuild import enqueue_backfill
    from app.modules.reporting.planner import ensure_bc_plans

    now = datetime.now(UTC)
    day = now.date()
    publish.publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=sync_seed.actor_id,
        accounts={"acc-1": publish.AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[ad_row("ad1", day)],
        requested_at=now,
    )
    ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.frozen_route = _route(sync_seed)
    assert (
        enqueue_backfill(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            start=day,
            end=day,
            now=now,
        )
        == 2
    )
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.kind for j in jobs} == {"ad_day", "creative_day"}
    assert all(j.advertiser_ids == ["acc-1"] for j in jobs)
    assert (
        enqueue_backfill(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            start=day,
            end=day,
            now=now,
        )
        == 0
    )
