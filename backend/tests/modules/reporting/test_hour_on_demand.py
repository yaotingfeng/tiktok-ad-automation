"""系列小时报只在用户打开小时趋势时按需拉取，热刷新不再定时拉。"""

from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.core.config import settings
from app.modules.reporting.planner import ensure_bc_plans
from app.modules.reporting.store_models import ReportSyncJob, ReportSyncState
from app.modules.reporting.trend import build_trend

# UTC 10-13 04:00 = 香港 10-13 12:00；acc-1 香港时区，本地今天 10-13。
NOW = datetime(2026, 10, 13, 4, tzinfo=UTC)
TODAY = date(2026, 10, 13)


def _hour_trend(session, seed, day, *, now=NOW):
    return build_trend(
        session,
        context=seed.context,
        bc_id="bc-a",
        level="account",
        advertiser_ids=("acc-1",),
        ids=("acc-1",),
        start=day,
        end=day,
        granularity="hour",
        now=now,
    )


def _hour_jobs(session, seed):
    return session.exec(
        select(ReportSyncJob).where(
            ReportSyncJob.tenant_id == seed.tenant_id,
            ReportSyncJob.kind == "campaign_hour",
        )
    ).all()


def _state(session, seed, day, status, at):
    session.add(
        ReportSyncState(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            report_kind="campaign_hour",
            stat_date=day,
            status=status,
            last_success_at=at if status == "COMPLETE" else None,
            updated_at=at,
        )
    )
    session.flush()


def test_missing_hours_queue_one_priority_job_and_report_syncing(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    ensure_bc_plans(session, now=NOW, tenant_id=sync_seed.tenant_id)
    first = _hour_trend(session, sync_seed, TODAY)
    assert first.syncing is True
    jobs = _hour_jobs(session, sync_seed)
    assert [(j.advertiser_ids, j.start_date, j.end_date, j.priority) for j in jobs] == [
        (["acc-1"], TODAY, TODAY, 1)
    ]
    # 轮询期间重复打开不会重复排队。
    assert _hour_trend(session, sync_seed, TODAY).syncing is True
    assert len(_hour_jobs(session, sync_seed)) == 1


def test_fresh_hours_do_not_refetch(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    ensure_bc_plans(session, now=NOW, tenant_id=sync_seed.tenant_id)
    _state(session, sync_seed, TODAY, "COMPLETE", NOW - timedelta(minutes=10))
    assert _hour_trend(session, sync_seed, TODAY).syncing is False
    assert _hour_jobs(session, sync_seed) == []


def test_open_day_refetches_after_hot_interval_but_closed_day_does_not(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    ensure_bc_plans(session, now=NOW, tenant_id=sync_seed.tenant_id)
    old = NOW - timedelta(hours=2)
    closed = TODAY - timedelta(days=3)
    _state(session, sync_seed, TODAY, "COMPLETE", old)
    _state(session, sync_seed, closed, "COMPLETE", old)
    assert _hour_trend(session, sync_seed, closed).syncing is False
    assert _hour_jobs(session, sync_seed) == []
    # 今天、昨天的小时数据仍在增长或被修正，超过热刷间隔就重新拉。
    assert _hour_trend(session, sync_seed, TODAY).syncing is True
    assert len(_hour_jobs(session, sync_seed)) == 1


def test_recent_failure_is_not_retried_on_every_poll(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    ensure_bc_plans(session, now=NOW, tenant_id=sync_seed.tenant_id)
    _state(session, sync_seed, TODAY, "FAILED", NOW - timedelta(minutes=5))
    trend = _hour_trend(session, sync_seed, TODAY)
    assert trend.syncing is False
    assert _hour_jobs(session, sync_seed) == []


def test_sync_disabled_queues_nothing(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    assert _hour_trend(session, sync_seed, TODAY).syncing is False
    assert _hour_jobs(session, sync_seed) == []


def test_day_trend_never_queues_hours(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    ensure_bc_plans(session, now=NOW, tenant_id=sync_seed.tenant_id)
    trend = build_trend(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        level="account",
        advertiser_ids=("acc-1",),
        ids=("acc-1",),
        start=TODAY,
        end=TODAY,
        granularity="day",
        now=NOW,
    )
    assert trend.syncing is False
    assert _hour_jobs(session, sync_seed) == []
