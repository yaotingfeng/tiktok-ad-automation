from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

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


class _EmptyReader:
    def bc_spend(self, **_kwargs):
        from app.integrations.tiktok.contracts.report_reader import RawReportPage

        return RawReportPage((), 1, 0, 0)

    def account_day(self, **_kwargs):
        from app.integrations.tiktok.contracts.report_reader import RawReportPage

        return RawReportPage((), 1, 0, 0)


def _delete_actor(factory, actor_id):
    """测试自建的用户不属于租户夹具清理范围，先删其名下行再删用户。"""
    from sqlmodel import SQLModel

    with factory() as s:
        for table in reversed(SQLModel.metadata.sorted_tables):
            if "actor_id" in table.c:
                s.exec(table.delete().where(table.c.actor_id == actor_id))
        user = SQLModel.metadata.tables["user"]
        s.exec(user.delete().where(user.c.id == actor_id))
        s.commit()


def test_platform_admin_discovery_actor_plans_directory(
    monkeypatch, committed_sync_seed, redis_client
):
    """平台管理员不是租户成员，代租户完成账户发现后，后台目录同步仍以其身份正常规划。"""
    from datetime import timedelta
    from uuid import uuid4

    from sqlmodel import select

    from app.core.db import engine
    from app.models import User
    from app.modules.accounts.models import DiscoveryRun
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    admin = User(
        username=f"admin-{uuid4().hex[:10]}", hashed_password="x", is_superuser=True
    )
    with factory() as s:
        s.add(admin)
        s.flush()
        s.add(
            DiscoveryRun(
                tenant_id=seed.tenant_id,
                actor_id=admin.id,
                connection_id=seed.connection.id,
                bc_id=None,
                status="COMPLETE",
                completed_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    try:
        job_tasks.plan_due_runs(
            database_engine=engine,
            redis_client=redis_client,
            now=datetime(2026, 10, 12, 3, tzinfo=UTC),
        )
        with factory() as s:
            assert s.get(AdSyncBc, (seed.tenant_id, "bc-a")).actor_id == admin.id
            runs = s.exec(
                select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).all()
            assert runs and {run.actor_id for run in runs} == {admin.id}
    finally:
        _delete_actor(factory, admin.id)


def test_unexpected_failure_in_one_bc_does_not_block_others(
    monkeypatch, committed_sync_seed, redis_client
):
    """单个 BC 规划出现意外错误时只推迟该 BC，其他 BC 本轮照常规划。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
    from app.modules.reporting.store_models import AdSyncBc

    seed, factory = committed_sync_seed
    with factory() as s:
        s.add(
            AdvertiserAccount(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-3",
                currency="USD",
                timezone="UTC",
                name="账户三",
            )
        )
        s.flush()
        s.add(
            BCAccountAccess(
                tenant_id=seed.tenant_id,
                bc_id="bc-b",
                advertiser_id="acc-3",
                connection_id=seed.connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                checked_at=datetime.now(UTC),
            )
        )
        s.commit()
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    original = job_tasks._plan_one

    def flaky(database_engine, redis_client, key, *args):
        if key[1] == "bc-a":
            raise RuntimeError("synthetic planning failure")
        return original(database_engine, redis_client, key, *args)

    monkeypatch.setattr(job_tasks, "_plan_one", flaky)
    now = datetime(2026, 10, 12, 3, tzinfo=UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        failed = s.get(AdSyncBc, (seed.tenant_id, "bc-a"))
        healthy = s.get(AdSyncBc, (seed.tenant_id, "bc-b"))
        assert failed.disabled_reason == "plan_failed"
        assert failed.next_hot_at >= now + timedelta(minutes=5)
        assert healthy.next_hot_at > now and healthy.disabled_reason is None


def test_directory_dispatch_skips_enqueued_runs_and_is_bounded(
    monkeypatch, committed_sync_seed, redis_client
):
    """已写入投递记录的目录运行不再重复入队；每次派发有上限，积压时规划仍保持轻量。"""
    from sqlmodel import Session, func, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 2)
    now = datetime(2026, 10, 12, 3, tzinfo=UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)

    def queued_runs():
        with factory() as s:
            return s.exec(
                select(func.count())
                .select_from(AdDirectoryRun)
                .where(AdDirectoryRun.tenant_id == seed.tenant_id)
            ).one()

    def enqueued():
        with factory() as s:
            return s.exec(
                select(func.count())
                .select_from(PendingDispatch)
                .where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_name == "ads.sync_step",
                )
            ).one()

    total = queued_runs()
    assert total > 2
    before = enqueued()
    with Session(engine) as s, s.begin():
        first = job_tasks._dispatch_directories(s, now)
    assert first <= 2
    assert enqueued() == before + first
    # 反复派发直到全部入队后，再派发不再产生任何重复入队。
    for _ in range(total):
        with Session(engine) as s, s.begin():
            job_tasks._dispatch_directories(s, now)
    assert enqueued() == total
    with Session(engine) as s, s.begin():
        assert job_tasks._dispatch_directories(s, now) == 0


@pytest.mark.parametrize("terminal", [False, True])
def test_lost_directory_message_is_redispatched_with_new_generation(
    monkeypatch, committed_sync_seed, redis_client, terminal
):
    """消息发布后长时间无人领取视为丢失：换新代数重新入队，旧消息执行时直接失效。"""
    from datetime import timedelta

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(
        job_tasks, "_reader_for", lambda **_kwargs: _ctx(_EmptyReader())
    )
    now = datetime(2026, 10, 12, 3, tzinfo=UTC)
    job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client, now=now)
    with factory() as s:
        run = s.exec(
            select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == seed.tenant_id)
        ).first()
        key = f"ads.sync_step:{run.id}:{run.claim_generation}:page:{run.next_page}"
        record = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == key,
            )
        ).one()
        if terminal:
            from app.modules.ads.sync_models import AdDirectoryPage

            s.add(
                AdDirectoryPage(
                    run_id=run.id,
                    page=1,
                    tenant_id=run.tenant_id,
                    advertiser_id=run.advertiser_id,
                    claim_generation=run.claim_generation,
                    complete=True,
                    items=[],
                    materials=[],
                )
            )
        record.published_at = now - timedelta(hours=1)
        s.commit()
        run_id, generation = run.id, run.claim_generation
    # 发布不久：仍在等待执行，不重复入队。
    with Session(engine) as s, s.begin():
        job_tasks._dispatch_directories(s, now)
    with factory() as s:
        assert s.get(AdDirectoryRun, run_id).claim_generation == generation
    later = now + job_tasks.DIRECTORY_MESSAGE_LOST_AFTER
    with Session(engine) as s, s.begin():
        job_tasks._dispatch_directories(s, later)
    with factory() as s:
        run = s.get(AdDirectoryRun, run_id)
        assert run.claim_generation == generation + 1
        dispatch = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key
                == f"ads.sync_step:{run_id}:{generation + 1}:page:{run.next_page}",
            )
        ).one()
        assert dispatch.task_name == (
            "ads.publish_step" if terminal else "ads.sync_step"
        )
        original = s.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == key,
            )
        ).one()
        assert original.task_name == "ads.sync_step"


def test_report_dispatch_keeps_small_window_and_rechecks_hot_priority(
    monkeypatch, committed_sync_seed
):
    from datetime import timedelta

    from sqlmodel import select

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import D, _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)

    def enqueue(session, index, tier):
        day = D - timedelta(days=index)
        return enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier=tier,
            advertiser_ids=["acc-1"],
            start=day,
            end=day,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            due_at=now,
        )

    with factory() as session, session.begin():
        for index in range(6):
            enqueue(session, index, "history")
    assert job_tasks._dispatch_pending(engine, now) == 2
    assert job_tasks._dispatch_pending(engine, now) == 0
    with factory() as session, session.begin():
        running = session.exec(
            select(ReportSyncJob).where(
                ReportSyncJob.tenant_id == seed.tenant_id,
                ReportSyncJob.status == "RUNNING",
            )
        ).all()
        assert len(running) == 2
        running[0].status = "COMPLETE"
        running[0].claim_token = running[0].claimed_until = None
        hot_id = enqueue(session, 20, "hot")
    assert job_tasks._dispatch_pending(engine, now) == 1
    with factory() as session:
        assert session.get(ReportSyncJob, hot_id).status == "RUNNING"


def test_report_worker_renews_lease_and_durably_wakes_dispatch(
    monkeypatch, committed_sync_seed, redis_client
):
    from datetime import timedelta

    from sqlmodel import select

    from app.core.db import engine
    from app.jobs.models import PendingDispatch
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import (
        D,
        FakeReader,
        _enqueue_and_claim,
        _route,
    )

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    claim = job.claim_token
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        row = session.get(ReportSyncJob, job.id)
        row.claimed_until = now + timedelta(seconds=1)
        next_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D - timedelta(days=1),
            end=D - timedelta(days=1),
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )

    @contextmanager
    def reader(**_kwargs):
        with factory() as session:
            assert session.get(ReportSyncJob, job.id).claimed_until >= now + timedelta(
                seconds=230
            )
        yield FakeReader()

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    monkeypatch.setattr(job_tasks, "_reader_for", reader)
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=claim,
        )
        == "COMPLETE"
    )
    with factory() as session:
        signal = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == f"report-refill:{job.id}:{claim}",
            )
        ).one()
        assert signal.task_name == "reporting.plan_due" and signal.payload == {
            "dispatch_only": True
        }

    def unexpected_reader(**_kwargs):
        raise AssertionError("补位仅领取本地任务，不能重新筛选或读取平台")

    monkeypatch.setattr(job_tasks, "_reader_for", unexpected_reader)
    assert (
        job_tasks.plan_due.run(
            tenant_id=str(seed.tenant_id),
            actor_id=str(seed.actor_id),
            payload=signal.payload,
        )
        == 1
    )
    with factory() as session:
        assert session.get(ReportSyncJob, next_id).status == "RUNNING"


def test_concurrent_report_dispatchers_cannot_overfill_window(
    monkeypatch, committed_sync_seed
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    from sqlalchemy import func
    from sqlmodel import select

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import D, _route

    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        for index in range(6):
            day = D - timedelta(days=index)
            enqueue_job(
                session,
                tenant_id=seed.tenant_id,
                bc_id="bc-a",
                kind="ad_day",
                tier="hot",
                advertiser_ids=["acc-1"],
                start=day,
                end=day,
                frozen_route=_route(seed),
                actor_id=seed.actor_id,
                due_at=now,
            )
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(lambda _n: job_tasks._dispatch_pending(engine, now), range(2))
        )
    assert sum(results) == 2
    with factory() as session:
        assert (
            session.exec(
                select(func.count())
                .select_from(ReportSyncJob)
                .where(
                    ReportSyncJob.tenant_id == seed.tenant_id,
                    ReportSyncJob.status == "RUNNING",
                )
            ).one()
            == 2
        )


def test_single_slot_refill_round_robins_three_tenants_across_rounds(monkeypatch):
    from contextlib import ExitStack
    from datetime import timedelta

    from sqlmodel import col, select

    from app.core.db import engine
    from app.modules.reporting.jobs import enqueue_job
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.sync_conftest import (
        committed_sync_seed as seed_fixture,
    )
    from tests.modules.reporting.test_jobs import D, _route

    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    with ExitStack() as stack:
        seeds = [
            stack.enter_context(contextmanager(seed_fixture.__wrapped__)())
            for _ in range(3)
        ]
        factory = seeds[0][1]
        now = datetime.now(UTC)
        with factory() as session, session.begin():
            for rank, (seed, _) in enumerate(seeds):
                for index in range(6):
                    day = D - timedelta(days=index)
                    enqueue_job(
                        session,
                        tenant_id=seed.tenant_id,
                        bc_id="bc-a",
                        kind="ad_day",
                        tier="hot",
                        advertiser_ids=["acc-1"],
                        start=day,
                        end=day,
                        frozen_route=_route(seed),
                        actor_id=seed.actor_id,
                        due_at=now - timedelta(minutes=3 - rank),
                    )
        assert job_tasks._dispatch_pending(engine, now) == 2
        with factory() as session:
            running = session.exec(
                select(ReportSyncJob)
                .where(ReportSyncJob.status == "RUNNING")
                .order_by(ReportSyncJob.due_at, ReportSyncJob.id)
            ).all()
            active = [r.id for r in running]
            seen = [r.tenant_id for r in running]
        for round_no in range(6):
            finished = active.pop(0)
            with factory() as session, session.begin():
                row = session.get(ReportSyncJob, finished)
                row.status = "COMPLETE"
                row.claim_token = row.claimed_until = None
            assert (
                job_tasks._dispatch_pending(
                    engine, now + timedelta(seconds=round_no + 1)
                )
                == 1
            )
            with factory() as session:
                fresh = session.exec(
                    select(ReportSyncJob).where(
                        ReportSyncJob.status == "RUNNING",
                        col(ReportSyncJob.id).not_in(active),
                    )
                ).one()
                active.append(fresh.id)
                seen.append(fresh.tenant_id)
        a, b, c = [s.tenant_id for s, _ in seeds]
        assert seen == [a, b, c, a, b, c, a, b]


@pytest.mark.parametrize("missing_lease", [False, True])
def test_expired_broker_claim_cannot_bypass_dispatch_window(
    monkeypatch, committed_sync_seed, redis_client, missing_lease
):
    """上线前积压的过期消息不能自行续租，绕过新窗口和热任务优先级。"""
    from datetime import timedelta

    from app.core.db import engine
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_jobs import _enqueue_and_claim

    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    with factory() as session, session.begin():
        session.get(ReportSyncJob, job.id).claimed_until = (
            None if missing_lease else datetime.now(UTC) - timedelta(seconds=1)
        )
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)

    def forbidden(**_kwargs):
        raise AssertionError("过期消息须由有界调度重新领取，不能自行恢复执行")

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
    with factory() as session:
        row = session.get(ReportSyncJob, job.id)
        assert row.status == "RUNNING"
        assert row.claimed_until is None or row.claimed_until < datetime.now(UTC)
        assert row.finished_at is None

    with factory() as session, session.begin():
        job_tasks._dispatch_report_jobs(session, datetime.now(UTC))
    with factory() as session:
        row = session.get(ReportSyncJob, job.id)
        assert row.claim_token != job.claim_token and row.claimed_until > datetime.now(
            UTC
        )
    # 新代数已经领取后，旧消息仍不可进入平台；不改变新代数的有效租约。
    assert (
        job_tasks.run_job_step(
            database_engine=engine,
            redis_client=redis_client,
            job_id=job.id,
            claim_token=job.claim_token,
        )
        == "FAILED"
    )
