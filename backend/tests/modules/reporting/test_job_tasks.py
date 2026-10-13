from contextlib import contextmanager
from datetime import UTC, datetime

from app.modules.reporting import job_tasks


def test_disabled_flag_plans_nothing(monkeypatch, committed_sync_seed, redis_client):
    _, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", False)
    from app.core.db import engine

    assert (
        job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client) == 0
    )


def test_plan_due_screens_with_bc_report_and_dispatches(
    monkeypatch, committed_sync_seed, redis_client
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    class Reader:
        def bc_spend(self, *, bc_id, start, end, page):
            from app.integrations.tiktok.contracts.report_reader import RawReportPage

            calls.append(bc_id)
            rows = (
                {
                    "dimensions": {"advertiser_id": "acc-1"},
                    "metrics": {"spend": "1.00"},
                },
            )
            return RawReportPage(rows, 1, 1, 1)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    from app.core.db import engine

    dispatched = job_tasks.plan_due_runs(
        database_engine=engine,
        redis_client=redis_client,
        now=datetime(2026, 10, 12, 3, tzinfo=UTC),
    )
    # 只为负责账户的主 BC 筛选；新计划首轮同时做热刷（1 天）与 32 天回刷，
    # 各筛选一次，ad_day 等任务已派发。
    assert calls == ["bc-a", "bc-a"] and dispatched >= 2


@contextmanager
def _ctx(obj):
    yield obj


def test_low_budget_slows_hot_and_material(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.reporting.planner import record_calls
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    now = datetime(2026, 10, 12, 3, tzinfo=UTC)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks.settings, "TIKTOK_REPORT_DAILY_CALL_BUDGET", 100)
    record_calls(redis_client, now, 75)
    from app.integrations.tiktok.contracts.report_reader import RawReportPage

    class Reader:
        def bc_spend(self, **_kwargs):
            return RawReportPage((), 1, 0, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_kwargs: _ctx(Reader()))
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        plan = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        assert plan.next_hot_at == now + timedelta(minutes=60)
        assert plan.next_material_at == now + timedelta(minutes=120)


def test_bc_permission_failure_falls_back_next_round(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from app.core.db import engine
    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    class Reader:
        def bc_spend(self, **_kwargs):
            calls.append("bc")
            raise DomainError("report_permission_denied", "报表权限不足")

        def account_day(self, *, advertiser_ids, **_kwargs):
            calls.append(advertiser_ids)
            return RawReportPage((), 1, 0, 0)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **_kwargs: _ctx(Reader()))
    now = datetime(2026, 10, 12, 3, tzinfo=UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        assert s.get(AdSyncBc, (seed.tenant_id, "bc-a")).bc_report_supported is False
    job_tasks.plan_due_runs(
        database_engine=engine,
        redis_client=redis_client,
        now=now + timedelta(minutes=5),
    )
    # 回退为账户报表筛选：热刷 1 个请求，32 天回刷按 30 天切成 2 个请求。
    assert calls == ["bc", *[("acc-1", "acc-2")] * 3]


def test_disabled_worker_never_opens_gateway(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", False)

    def forbidden(**_kwargs):
        raise AssertionError("不允许外部读取")

    monkeypatch.setattr(job_tasks, "_reader_for", forbidden)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "WAITING"
    )


def test_gateway_admission_defers_without_failure(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def deferred(**_kwargs):
        raise AccountAdmissionDeferred(300000)

    monkeypatch.setattr(job_tasks, "_reader_for", deferred)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "WAITING"
    )
    with factory() as s:
        row = s.get(ReportSyncJob, job.id)
        assert row.status == "WAITING" and row.attempts == 0


def test_cleanup_removes_expired_jobs_hours_and_snapshot_rows(session, sync_seed):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow
    from app.modules.reporting.store_models import (
        CampaignReportHour,
        ReportBalanceRun,
        ReportSyncJob,
    )
    from tests.modules.reporting.test_jobs import D, _route

    now = datetime.now(UTC)
    job_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="hot",
        advertiser_ids=["acc-1"],
        start=D,
        end=D,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    job = session.get(ReportSyncJob, job_id)
    job.status = "COMPLETE"
    job.finished_at = now - timedelta(days=8)
    snapshot = QuerySnapshot(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        actor_id=sync_seed.actor_id,
        filter_digest="x" * 64,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        total=1,
    )
    session.add(snapshot)
    session.flush()
    row = QuerySnapshotRow(
        snapshot_id=snapshot.id,
        row_key="old",
        stable_sequence=0,
        dimension="ad",
        membership_digest="x" * 64,
    )
    session.add(row)
    session.add(
        CampaignReportHour(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            stat_hour=now - timedelta(days=31),
            campaign_id="c1",
            currency="USD",
            timezone="UTC",
            fetched_at=now,
            sync_job_id=uuid4(),
        )
    )
    session.flush()
    runs = []
    for status in ("COMPLETE", "FAILED", "ACTIVE"):
        run = ReportBalanceRun(
            tenant_id=sync_seed.tenant_id,
            actor_id=sync_seed.actor_id,
            run_key=f"old-{status}",
            frozen_route=_route(sync_seed),
            remaining_advertiser_ids=["acc-1"],
            status=status,
            created_at=now - timedelta(days=8),
        )
        session.add(run)
        runs.append((run.id, status))
    session.flush()
    snapshot_id = snapshot.id
    assert job_tasks.cleanup_store(session, now=now) == {
        "jobs": 1,
        "hours": 1,
        "snapshots": 1,
        "balances": 2,
    }
    session.expire_all()
    assert session.get(QuerySnapshotRow, (snapshot_id, "old")) is None
    assert session.get(ReportSyncJob, job_id) is None
    for run_id, status in runs:
        assert (session.get(ReportBalanceRun, run_id) is not None) == (
            status == "ACTIVE"
        )


def test_revoked_actor_fails_job_before_gateway(
    monkeypatch, committed_sync_seed, redis_client
):
    from app.core.db import engine
    from app.modules.tenants.models import TenantMembership
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    with factory() as s:
        s.get(TenantMembership, (seed.tenant_id, seed.actor_id)).active = False
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def forbidden(**_kwargs):
        raise AssertionError("撤权后不得读取")

    monkeypatch.setattr(job_tasks, "_reader_for", forbidden)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "FAILED"
    )
