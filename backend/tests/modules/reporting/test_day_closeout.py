"""当地昨日收尾：真实 PG 水位、时区、任务去重与实时容量隔离。"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.modules.reporting import job_tasks, planner
from app.modules.reporting.jobs import claim_jobs, enqueue_job, execute_job
from app.modules.reporting.lanes import report_workload
from app.modules.reporting.store_models import (
    AdReportDay,
    AdSyncBc,
    MaterialReportDay,
    ReportSyncJob,
    ReportSyncState,
)

NOW = datetime(2026, 10, 21, 16, 1, tzinfo=UTC)


def seed_yesterday(session, seed, *, now=NOW, advertiser="acc-1", kind="ad_day"):
    tz = "Asia/Hong_Kong" if advertiser == "acc-1" else "UTC"
    today = planner.local_today(tz, now)
    yesterday = today - timedelta(days=1)
    midnight = datetime.combine(today, time.min, ZoneInfo(tz))
    common = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": advertiser,
        "stat_date": yesterday,
        "currency": "USD",
        "timezone": tz,
        "fetched_at": midnight - timedelta(minutes=5),
        "sync_job_id": uuid4(),
        "spend": Decimal("1"),
    }
    if kind == "ad_day":
        fact = AdReportDay(ad_id="ad1", campaign_id="c1", adgroup_id="g1", **common)
    else:
        fact = MaterialReportDay(
            main_material_id="p1", main_material_type="VIDEO", **common
        )
    session.add(fact)
    session.add(
        ReportSyncState(
            tenant_id=seed.tenant_id,
            advertiser_id=advertiser,
            stat_date=yesterday,
            report_kind=kind,
            status="COMPLETE",
            row_count=1,
            last_success_at=midnight - timedelta(minutes=5),
        )
    )
    session.flush()
    return yesterday, midnight


def plan_for(session, seed, now=NOW):
    planner.ensure_bc_plans(session, now=now, tenant_id=seed.tenant_id)
    return session.get(AdSyncBc, (seed.tenant_id, "bc-a"))


@pytest.mark.parametrize("kind", ["ad_day", "material_day"])
def test_first_hot_round_closes_yesterday_once_for_each_timezone(
    session, sync_seed, kind
):
    plan = plan_for(session, sync_seed)
    days = {
        a: seed_yesterday(session, sync_seed, advertiser=a, kind=kind)[0]
        for a in ("acc-1", "acc-2")
    }
    assert days["acc-1"] != days["acc-2"]
    assert planner.plan_day_closeout(session, plan, now=NOW) == 4
    assert (
        planner.plan_day_closeout(session, plan, now=NOW + timedelta(minutes=15)) == 0
    )
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(jobs) == 4
    for job in jobs:
        assert job.kind in {"ad_day", "material_day"}
        assert job.start_date == job.end_date == days[job.advertiser_ids[0]]
        assert report_workload(session, job, now=NOW) == "live"
        assert (
            report_workload(session, job, now=NOW + timedelta(days=1)) == "background"
        )


def test_closeout_skips_no_facts_and_already_successful_kinds(session, sync_seed):
    plan = plan_for(session, sync_seed)
    assert planner.plan_day_closeout(session, plan, now=NOW) == 0
    day, midnight = seed_yesterday(session, sync_seed)
    state = session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", "ad_day", day))
    state.last_success_at = midnight + timedelta(seconds=1)
    session.flush()
    assert planner.plan_day_closeout(session, plan, now=NOW) == 1
    jobs = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    assert [(j.kind, j.advertiser_ids) for j in jobs] == [("material_day", ["acc-1"])]
    # bc-b 可见 acc-2，但不拥有它，不能生成重复收尾。
    seed_yesterday(session, sync_seed, advertiser="acc-2")
    other = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    assert planner.plan_day_closeout(session, other, now=NOW) == 0


def test_closeout_is_planned_by_due_hot_even_without_today_activity(
    monkeypatch, committed_sync_seed, redis_client
):
    from contextlib import contextmanager

    from app.core.db import engine
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        plan = plan_for(session, seed)
        seed_yesterday(session, seed)
        plan.next_daily_at = plan.next_directory_at = NOW + timedelta(days=1)

    @contextmanager
    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(300000)
        yield  # pragma: no cover

    monkeypatch.setattr(job_tasks, "_reader_for", deferred)
    job_tasks._plan_one(
        engine,
        redis_client,
        (seed.tenant_id, "bc-a"),
        NOW,
        NOW + timedelta(seconds=20),
        planner.HOT_INTERVAL,
        planner.MATERIAL_INTERVAL,
        allow_backfill=False,
    )
    with factory() as session:
        jobs = session.exec(
            select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id)
        ).all()
        assert {j.kind for j in jobs} == {"ad_day", "material_day"}
        assert all(j.start_date == NOW.date() for j in jobs)  # 香港已经进入次日。


def test_closeout_capacity_does_not_consume_today_slots(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting.jobs import LEASE

    plan = plan_for(session, sync_seed)
    seed_yesterday(session, sync_seed)
    planner.plan_day_closeout(session, plan, now=NOW)
    closeouts = session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all()
    for job in closeouts:
        job.status, job.claim_token, job.claimed_until = "RUNNING", uuid4(), NOW + LEASE
    today = planner.local_today("Asia/Hong_Kong", NOW)
    live_id = enqueue_job(
        session,
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-1"],
        start=today,
        end=today,
        due_at=NOW,
        **planner._common(plan),
    )
    monkeypatch.setattr(job_tasks, "REPORT_DISPATCH_WINDOW", 1)
    assert job_tasks._dispatch_report_jobs(session, NOW, allow_backfill=False) == 1
    session.refresh(session.get(ReportSyncJob, live_id))
    assert session.get(ReportSyncJob, live_id).status == "RUNNING"


def test_closeout_publishes_success_and_daily_refresh_can_replace_it(
    committed_sync_seed,
):
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from tests.modules.reporting.sync_conftest import ad_row

    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        plan = plan_for(session, seed, now)
        day, midnight = seed_yesterday(session, seed, now=now, advertiser="acc-2")
        planner.plan_day_closeout(session, plan, now=now)
        claimed = claim_jobs(
            session, now=now + timedelta(seconds=1), limit=10, lane="closeout"
        )
        assert len(claimed) == 2

    class Reader:
        spend = "2"

        def ad_day(self, **_kwargs):
            return RawReportPage(
                (ad_row("ad1", day, adv="acc-2", spend=self.spend),), 1, 1, 1
            )

        def material_day(self, **_kwargs):
            return RawReportPage((), 1, 0, 0)

    reader = Reader()
    for job in claimed:
        assert (
            execute_job(
                factory, job_id=job.id, claim_token=job.claim_token, reader=reader
            )
            == "COMPLETE"
        )
    with factory() as session, session.begin():
        plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        for kind in ("ad_day", "material_day"):
            state = session.get(ReportSyncState, (seed.tenant_id, "acc-2", kind, day))
            assert state.status == "COMPLETE" and state.last_success_at > midnight
        assert planner.plan_day_closeout(session, plan, now=now) == 0
        assert planner.plan_daily(session, plan, active_32d=["acc-2"], now=now) > 0
        daily = next(
            j
            for j in session.exec(
                select(ReportSyncJob).where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.priority == 3,
                )
            ).all()
            if j.kind == "ad_day" and j.start_date <= day <= j.end_date
        )
        daily.status, daily.claim_token = "RUNNING", uuid4()
        daily.claimed_until = now + timedelta(minutes=4)
    reader.spend = "3"
    assert (
        execute_job(
            factory, job_id=daily.id, claim_token=daily.claim_token, reader=reader
        )
        == "COMPLETE"
    )
    with factory() as session:
        fact = session.get(AdReportDay, (seed.tenant_id, "acc-2", day, "ad1"))
        assert fact.spend == Decimal("3") and fact.sync_job_id == daily.id


def test_pre_midnight_hot_job_does_not_swallow_closeout(session, sync_seed):
    plan = plan_for(session, sync_seed)
    day, _ = seed_yesterday(session, sync_seed)
    enqueue_job(
        session,
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-1"],
        start=day,
        end=day,
        due_at=NOW - timedelta(minutes=10),
        **planner._common(plan),
    )
    assert planner.plan_day_closeout(session, plan, now=NOW) == 2
    assert (
        planner.plan_day_closeout(session, plan, now=NOW + timedelta(minutes=15)) == 0
    )
    claimed = claim_jobs(session, now=NOW, limit=10, lane="closeout")
    assert len(claimed) == 2


@pytest.mark.parametrize("timezone", ["America/New_York", "Asia/Kathmandu"])
def test_closeout_midnight_uses_account_timezone_including_dst(
    session, sync_seed, timezone
):
    from app.modules.accounts.models import AdvertiserAccount

    now = datetime(2026, 11, 2, 5, 1, tzinfo=UTC)
    plan = plan_for(session, sync_seed, now)
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-2"))
    account.timezone = timezone
    today = planner.local_today(timezone, now)
    day = today - timedelta(days=1)
    midnight = datetime.combine(today, time.min, ZoneInfo(timezone))
    from app.modules.reporting.partitions import ensure_month_partitions_for_range

    ensure_month_partitions_for_range(session, day, day)
    session.add(
        AdReportDay(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-2",
            stat_date=day,
            ad_id="dst-ad",
            campaign_id="c1",
            adgroup_id="g1",
            currency="USD",
            timezone=timezone,
            fetched_at=midnight - timedelta(minutes=5),
            sync_job_id=uuid4(),
            spend=Decimal("1"),
        )
    )
    state = ReportSyncState(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-2",
        stat_date=day,
        report_kind="ad_day",
        status="COMPLETE",
        row_count=1,
        last_success_at=midnight - timedelta(seconds=1),
    )
    session.add(state)
    session.flush()
    assert planner.plan_day_closeout(session, plan, now=now) == 2
    # 成功必须越过真实当地零点；不是以 UTC 日期或固定 24 小时反推。
    state.last_success_at = midnight + timedelta(seconds=1)
    session.flush()
    for j in session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all():
        j.status = "COMPLETE"
    assert planner.plan_day_closeout(session, plan, now=now) == 0


@pytest.mark.parametrize("status", ["WAITING", "FAILED", "COMPLETE"])
def test_closeout_is_not_recreated_after_initial_plan(session, sync_seed, status):
    plan = plan_for(session, sync_seed)
    seed_yesterday(session, sync_seed)
    assert planner.plan_day_closeout(session, plan, now=NOW) == 2
    for job in session.exec(
        select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)
    ).all():
        job.status = status
    session.flush()
    # 失败交由原任务重试/告警和每日回刷；不能每 15 分钟重新制造一整轮收尾。
    assert (
        planner.plan_day_closeout(session, plan, now=NOW + timedelta(minutes=15)) == 0
    )
