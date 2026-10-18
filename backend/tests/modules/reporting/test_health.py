from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.modules.reporting import planner
from app.modules.reporting.health import observe_health
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealthEvent,
    ReportSyncJob,
    ReportSyncState,
)


def test_missing_current_data_is_degraded_and_repaired_without_fake_success(
    session, sync_seed
):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    p = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    p.last_active_advertiser_ids = ["acc-2"]
    p.active_screened_at = now
    health = observe_health(session, p, now=now)
    assert health.status == "DEGRADED"
    assert (
        health.metrics["stale_ad_accounts"]
        == health.metrics["stale_material_accounts"]
        == 1
    )
    assert health.metrics["missing_directory_ads"] == 0
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert {j.kind for j in jobs} == {"ad_day", "material_day"}
    assert all(j.advertiser_ids == ["acc-2"] for j in jobs)
    assert (
        session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-2", "ad_day", now.date())
        )
        is None
    )
    observe_health(session, p, now=now + timedelta(seconds=60))
    assert len(session.exec(select(ReportSyncHealthEvent)).all()) == 1
    assert len(session.exec(select(ReportSyncJob)).all()) == 2
    for kind in ["ad_day", "material_day"]:
        session.add(
            ReportSyncState(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-2",
                report_kind=kind,
                stat_date=now.date(),
                status="COMPLETE",
                last_success_at=now + timedelta(minutes=1),
            )
        )
    session.flush()
    health = observe_health(session, p, now=now + timedelta(minutes=2))
    assert health.status == "HEALTHY" and not health.issues
    assert len(session.exec(select(ReportSyncHealthEvent)).all()) == 2


def test_no_spend_scope_does_not_raise_false_staleness(session, sync_seed):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    p = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    p.last_active_advertiser_ids = ["acc-1"]  # 不属于该 BC 的规划所有权，不得越界修复。
    health = observe_health(session, p, now=now)
    assert health.status == "HEALTHY"
    assert session.exec(select(ReportSyncJob)).all() == []


def test_unknown_empty_active_scope_is_not_healthy(session, sync_seed):
    now = datetime.now(UTC)
    planner.ensure_bc_plans(session, now=now, tenant_id=sync_seed.tenant_id)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan.last_active_advertiser_ids = []
    plan.active_screened_at = None
    health = observe_health(session, plan, now=now)
    assert health.status == "DEGRADED"
    assert "ACCOUNT_SCREEN_STALE" in health.issues
    jobs = session.exec(select(ReportSyncJob)).all()
    assert {adv for job in jobs for adv in job.advertiser_ids} == {"acc-1", "acc-2"}
    assert all(job.kind == "ad_day" for job in jobs)
