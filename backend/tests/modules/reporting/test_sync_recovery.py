from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.db import engine
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs import outbox
from app.jobs.admission import AdmissionPolicy, admission_keys, admit_call, release_call
from app.jobs.celery_app import celery_app
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.jobs.outbox import enqueue_after_commit, flush_dispatch
from app.modules.reporting.sync import collect_report_step
from app.modules.reporting.sync_models import ReportSyncRun
from app.modules.reporting.tasks import TASK_NAME


class _Result:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row
        self.rolled_back = False

    def exec(self, _statement):
        return _Result(self.row)

    def rollback(self):
        self.rolled_back = True

    def flush(self):
        return None


def test_old_generation_stops_before_transport_and_does_not_publish():
    run = SimpleNamespace(
        id=uuid4(), claim_generation=2, status="RUNNING", coverage="PENDING", error_code=None
    )
    session = _Session(run)

    class Transport:
        def read_page(self, _query):
            raise AssertionError("stale generation must not call provider")

    assert collect_report_step(
        session, run_id=run.id, claim_generation=1, gateway=SimpleNamespace(reports=Transport())
    ) == "FAILED"
    assert run.status == "RUNNING"
    assert run.coverage == "PENDING"
    assert session.rolled_back is False


@pytest.fixture
def recovery_run(session, reporting_seed):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    query = {
        "advertiser_id": "report-account",
        "report_contract": "basic_campaign",
        "metric_family": "delivery",
        "dimensions": ["campaign_id", "stat_time_day"],
        "metrics": ["spend"],
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "granularity": "DAY",
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "filter_ids": [],
        "page": 1,
    }
    run = ReportSyncRun(
        tenant_id=reporting_seed.context.tenant_id,
        advertiser_id="report-account",
        bc_id=route.bc_id,
        actor_id=reporting_seed.context.actor_id,
        connection_id=route.connection_id,
        channel=route.channel,
        frozen_route=route.model_dump(mode="json"),
        request_id=uuid4(),
        partition_key="a" * 64,
        query=query,
        claim_generation=2,
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


def test_old_generation_is_fenced_in_real_postgresql(session, recovery_run):
    run_id = recovery_run.id

    class Transport:
        def read_page(self, _query):
            raise AssertionError("stale generation must not call provider")

    assert collect_report_step(
        session,
        run_id=run_id,
        claim_generation=1,
        gateway=SimpleNamespace(reports=Transport()),
    ) == "FAILED"
    persisted = session.exec(select(ReportSyncRun).where(ReportSyncRun.id == run_id)).one()
    assert persisted.claim_generation == 2
    assert persisted.status == "RUNNING"


def test_async_create_bucket_is_per_advertiser_and_release_keeps_rate(redis_client):
    policy = AdmissionPolicy(
        app_max_inflight=20,
        endpoint_max_inflight=20,
        tenant_max_inflight=20,
        advertiser_max_inflight=20,
        app_calls_per_window=1000,
        endpoint_calls_per_window=1000,
        window_ms=1000,
        lease_ms=1000,
        advertiser_calls_per_window=2,
        advertiser_window_ms=3_600_000,
    )
    scope = {
        "app_scope": f"recovery-{uuid4()}",
        "endpoint": "reports.task_create",
        "tenant_id": uuid4(),
        "advertiser_id": "a",
    }
    leases = [uuid4() for _ in range(2)]
    other = scope | {"advertiser_id": "b"}
    try:
        for lease in leases:
            assert admit_call(redis_client, **scope, lease_id=lease, policy=policy).granted
        assert not admit_call(redis_client, **scope, lease_id=uuid4(), policy=policy).granted
        assert admit_call(redis_client, **other, lease_id=uuid4(), policy=policy).granted
        release_call(redis_client, **scope, lease_id=leases[0])
        # Releasing inflight does not refund the hourly creation bucket.
        assert not admit_call(redis_client, **scope, lease_id=uuid4(), policy=policy).granted
    finally:
        keys = admission_keys(**scope, async_create=True)
        redis_client.delete(*keys)
        redis_client.delete(*admission_keys(**other, async_create=True))


def test_successor_available_at_is_a_real_outbox_gate(monkeypatch):
    """A WAIT continuation is invisible to the publisher until its due time."""

    context = TenantContext(uuid4(), uuid4(), "operator")
    with Session(engine) as session:
        session.exec(
            PendingDispatch.__table__.delete().where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        )
        session.exec(
            DispatchTenantCursor.__table__.delete().where(
                DispatchTenantCursor.tenant_id == context.tenant_id
            )
        )
        due = datetime.now(UTC) + timedelta(seconds=30)
        dispatch_id = enqueue_after_commit(
            session,
            context=context,
            task_name=TASK_NAME,
            task_key=f"wait:{uuid4()}",
            payload={"run_id": str(uuid4()), "claim_generation": 1},
        )
        dispatch = session.get(PendingDispatch, dispatch_id)
        assert dispatch is not None
        dispatch.available_at = due
        session.commit()

    sent = []
    monkeypatch.setattr(outbox, "engine", engine)
    monkeypatch.setattr(
        "app.jobs.queued_dispatches.queued_dispatches",
        lambda: SimpleNamespace(contains=lambda **_: False),
    )
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: sent.append(kwargs)
    )
    try:
        assert flush_dispatch() == 0
        with Session(engine) as session:
            row = session.get(PendingDispatch, dispatch_id)
            assert row is not None
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        assert flush_dispatch() == 1
        assert sent
    finally:
        with Session(engine) as session:
            row = session.get(PendingDispatch, dispatch_id)
            if row is not None:
                session.delete(row)
            cursor = session.get(DispatchTenantCursor, context.tenant_id)
            if cursor is not None:
                session.delete(cursor)
            session.commit()



def test_worker_wait_and_successor_respect_persisted_due(
    session, reporting_seed, recovery_run, redis_client, monkeypatch
):
    from app.modules.reporting import tasks

    now = datetime.now(UTC)
    recovery_run.task_id = "remote-task-1"
    recovery_run.task_status = "RUNNING"
    recovery_run.status = "WAITING_REMOTE"
    recovery_run.next_attempt_at = now + timedelta(seconds=30)
    session.flush()
    route_before = dict(recovery_run.frozen_route)
    calls = []

    class Reports:
        def check_task(self, task):
            calls.append(task.task_id)
            return task

    @contextmanager
    def gateway(**kwargs):
        assert kwargs["route"].model_dump(mode="json") == route_before
        yield SimpleNamespace(reports=Reports())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    args = {
        "database_engine": session.connection(),
        "redis_client": redis_client,
        "context": reporting_seed.context,
        "run_id": recovery_run.id,
        "claim_generation": 2,
    }
    # A duplicate broker message bypassing publisher cannot claim before due.
    assert tasks.run_report_step(**args) == "WAIT"
    session.refresh(recovery_run)
    assert recovery_run.claim_token is None
    assert recovery_run.claimed_until is None
    assert calls == []
    assert not session.exec(select(PendingDispatch).where(
        PendingDispatch.tenant_id == reporting_seed.context.tenant_id
    )).all()

    recovery_run.next_attempt_at = now - timedelta(seconds=1)
    session.flush()
    assert tasks.run_report_step(**args) == "WAIT"
    session.refresh(recovery_run)
    assert calls == ["remote-task-1"]
    successor = session.exec(select(PendingDispatch).where(
        PendingDispatch.tenant_id == reporting_seed.context.tenant_id
    )).one()
    assert recovery_run.next_attempt_at > now
    assert successor.available_at == recovery_run.next_attempt_at
    assert successor.payload == {"run_id": str(recovery_run.id), "claim_generation": 2}
    assert successor.task_key == tasks._successor_key(recovery_run, claim_generation=2)
    assert recovery_run.frozen_route == route_before
    assert tasks.run_report_step(**args) == "WAIT"
    assert calls == ["remote-task-1"]

    # Terminals and stale generations short circuit even with an elapsed due.
    recovery_run.status = "COMPLETE"
    recovery_run.next_attempt_at = now - timedelta(seconds=1)
    session.flush()
    assert tasks.run_report_step(**args) == "READY"
    assert tasks.run_report_step(**(args | {"claim_generation": 1})) == "FAILED"
    assert calls == ["remote-task-1"]



def test_rebound_route_stops_before_gateway(
    session, reporting_seed, recovery_run, redis_client, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import TikTokConnection
    from app.modules.reporting import tasks

    binding = session.exec(select(BCConnectionBinding).where(
        BCConnectionBinding.tenant_id == reporting_seed.context.tenant_id,
        BCConnectionBinding.bc_id == "bc-report",
    )).one()
    # 模拟 BC 默认连接/通道被重新绑定；运行的 frozen route 仍指向原连接。
    replacement = TikTokConnection(
        tenant_id=reporting_seed.context.tenant_id,
        status="ACTIVE", kind="OFFICIAL_MCP", authorization_revision=0,
        adapter_contract_revision="mcp-v1",
    )
    session.add(replacement)
    session.flush()
    session.delete(binding)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=reporting_seed.context.tenant_id,
            bc_id="bc-report", connection_id=replacement.id,
            kind="OFFICIAL_MCP", status="ACTIVE", authorization_revision=0,
            revision=1,
        )
    )
    session.flush()
    frozen = dict(recovery_run.frozen_route)

    def forbidden(**_kwargs):
        raise AssertionError("changed binding must stop before gateway")

    monkeypatch.setattr(tasks, "open_tiktok_gateway", forbidden)
    with pytest.raises(DomainError):
        tasks.run_report_step(
            database_engine=session.connection(),
            redis_client=redis_client,
            context=reporting_seed.context,
            run_id=recovery_run.id,
            claim_generation=2,
        )
    # Failed verification rolls back the attempted claim; route is never replaced.
    session.refresh(recovery_run)
    assert recovery_run.frozen_route == frozen
    assert recovery_run.claimed_until is None


@pytest.fixture
def committed_recovery():
    """独立提交的租户夹具，使不同物理连接真实竞争同一运行行。"""
    from sqlmodel import SQLModel

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.ads import tasks as directory_tasks
    from app.modules.ads.sync_models import AdDirectoryRun
    from tests.modules.conftest import create_context
    from tests.modules.reporting.conftest import reporting_seed as seed_fixture

    assert directory_tasks.TASK_NAME == "ads.sync_step"

    with Session(engine) as db:
        context = create_context(db)
        db.add(AdvertiserAccount(
            tenant_id=context.tenant_id, advertiser_id="report-account",
            currency="USD", timezone="UTC",
        ))
        db.flush()
        seed = seed_fixture.__wrapped__(db, context, None)
        report = recovery_run.__wrapped__(db, seed)
        report.task_id = "persisted-async-task"
        report.task_status = "RUNNING"
        directory = AdDirectoryRun(
            tenant_id=context.tenant_id, advertiser_id=report.advertiser_id,
            bc_id=report.bc_id, actor_id=context.actor_id,
            connection_id=report.connection_id, channel=report.channel,
            frozen_route=dict(report.frozen_route), partition_key="b" * 64,
            kind="campaign", ad_type="REGULAR", claim_generation=1,
            status="STALE", query={
                "advertiser_id": report.advertiser_id,
                "kind": "campaign", "ad_type": "REGULAR", "page_size": 100,
                "ids": [], "parent_ids": [], "include_deleted": False,
            },
        )
        db.add(directory)
        db.flush()
        result = SimpleNamespace(
            context=context, report_id=report.id, directory_id=directory.id,
            route=dict(report.frozen_route),
        )
        db.commit()
    try:
        yield result
    finally:
        # 只清除该测试随机租户的已提交事实，不能 rollback 其他连接的事务。
        with Session(engine) as db:
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    db.execute(table.delete().where(table.c.tenant_id == context.tenant_id))
            tenant = SQLModel.metadata.tables["tenant"]
            user = SQLModel.metadata.tables["user"]
            db.execute(tenant.delete().where(tenant.c.id == context.tenant_id))
            db.execute(user.delete().where(user.c.id == context.actor_id))
            db.commit()


def _worker_args(seed, redis_client, *, directory=False, generation=2):
    return {
        "database_engine": engine,
        "redis_client": redis_client,
        "context": seed.context,
        "run_id": seed.directory_id if directory else seed.report_id,
        "claim_generation": generation,
    }


def test_two_workers_claim_once_and_commit_one_wait_successor(
    committed_recovery, redis_client, redis_key_prefix, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from app.modules.reporting import tasks

    seed = committed_recovery
    entered, resume, calls = [f"{redis_key_prefix}:{name}" for name in ("entered", "resume", "calls")]

    class Reports:
        def check_task(self, task):
            redis_client.incr(calls)
            return task

    @contextmanager
    def gateway(**kwargs):
        assert kwargs["route"].model_dump(mode="json") == seed.route
        redis_client.lpush(entered, "claimed")
        assert redis_client.blpop(resume, timeout=10) is not None
        yield SimpleNamespace(reports=Reports())

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)
    args = _worker_args(seed, redis_client)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(tasks.run_report_step, **args)
            try:
                assert redis_client.blpop(entered, timeout=10) is not None
                # 第一连接已提交领取、尚未进入 provider；第二连接必须被 lease 拦住。
                second = pool.submit(tasks.run_report_step, **args)
                assert second.result(timeout=5) == "WAIT"
                with Session(engine) as observer:
                    row = observer.get(ReportSyncRun, seed.report_id)
                    assert row.claim_token is not None
                    assert row.claimed_until > datetime.now(UTC)
                    assert observer.exec(select(PendingDispatch).where(
                        PendingDispatch.tenant_id == seed.context.tenant_id
                    )).all() == []
            finally:
                redis_client.lpush(resume, "continue")
            assert first.result(timeout=10) == "WAIT"
        # 独立会话同时读到 WAIT 与唯一 successor；重投不能生成第二条。
        assert tasks.run_report_step(**args) == "WAIT"
        with Session(engine) as observer:
            row = observer.get(ReportSyncRun, seed.report_id)
            successor = observer.exec(select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.context.tenant_id
            )).one()
            assert row.status == "WAITING_REMOTE"
            assert row.claim_token is None and row.claimed_until is None
            assert successor.available_at == row.next_attempt_at
            assert successor.payload == {"run_id": str(row.id), "claim_generation": 2}
            assert row.frozen_route == seed.route
        assert redis_client.get(calls) == "1"
    finally:
        redis_client.delete(entered, resume, calls)


@pytest.mark.parametrize("directory", [False, True], ids=["report", "directory"])
def test_expired_scanner_takes_new_generation_and_fences_old_worker(
    committed_recovery, redis_client, monkeypatch, directory
):
    """真实 scanner 接管过期 lease；旧消息不能触发 provider 或写发布事实。"""

    from app.modules.ads import tasks as directory_tasks
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting import tasks

    seed = committed_recovery
    model = AdDirectoryRun if directory else ReportSyncRun
    run_id = seed.directory_id if directory else seed.report_id
    old_generation = 1 if directory else 2
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    with Session(engine) as db:
        row = db.get(model, run_id)
        row.status = "RUNNING"
        row.claim_generation = old_generation
        row.claim_token = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    assert tasks.scan_due_runs(database_engine=engine) >= 1
    with Session(engine) as db:
        row = db.get(model, run_id)
        assert row.claim_generation == old_generation + 1
        assert row.claim_token is None
        dispatches = db.exec(select(PendingDispatch).where(
            PendingDispatch.tenant_id == seed.context.tenant_id
        )).all()
        assert any(item.payload.get("run_id") == str(run_id) for item in dispatches)
        new_generation = row.claim_generation

    calls = []
    if directory:
        def forbidden_directory(**_kwargs):
            calls.append("directory")
            raise AssertionError("stale directory worker must not call provider")

        monkeypatch.setattr(directory_tasks, "open_tiktok_gateway", forbidden_directory)
        result = directory_tasks.run_directory_step(
            database_engine=engine, redis_client=redis_client,
            context=seed.context, run_id=run_id,
            claim_generation=old_generation,
        )
    else:
        def forbidden_report(**_kwargs):
            calls.append("report")
            raise AssertionError("stale report worker must not call provider")

        monkeypatch.setattr(tasks, "open_tiktok_gateway", forbidden_report)
        result = tasks.run_report_step(
            database_engine=engine, redis_client=redis_client,
            context=seed.context, run_id=run_id,
            claim_generation=old_generation,
        )
    assert result == "FAILED"
    assert calls == []
    with Session(engine) as db:
        row = db.get(model, run_id)
        assert row.claim_generation == new_generation
        assert row.published_version is None

    if directory:
        @contextmanager
        def gateway(**_kwargs):
            class Ads:
                def read_page(self, _query):
                    return DirectoryPage(
                        items=(), materials=(), next_page=None, complete=True,
                        evidence=CallEvidence(request_id="directory-recovery"),
                    )

            yield SimpleNamespace(ads=Ads())

        monkeypatch.setattr(directory_tasks, "open_tiktok_gateway", gateway)
        assert directory_tasks.run_directory_step(
            database_engine=engine, redis_client=redis_client,
            context=seed.context, run_id=run_id,
            claim_generation=new_generation,
        ) == "READY"
        with Session(engine) as db:
            assert db.get(AdDirectoryRun, run_id).published_version is not None


@pytest.mark.parametrize("directory", [False, True], ids=["report", "directory"])
def test_scanner_does_not_overwrite_another_sessions_locked_claim(
    committed_recovery, monkeypatch, directory
):
    from concurrent.futures import ThreadPoolExecutor

    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting import tasks

    seed = committed_recovery
    model = AdDirectoryRun if directory else ReportSyncRun
    run_id = seed.directory_id if directory else seed.report_id
    monkeypatch.setattr(tasks.settings, "ADS_SYNC_ENABLED", True)
    with Session(engine) as db:
        report = db.get(ReportSyncRun, seed.report_id)
        report.status = "STALE" if directory else "RUNNING"
        row = db.get(model, run_id)
        row.status = "RUNNING"
        row.claim_token = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as owner:
        row = owner.exec(select(model).where(model.id == run_id).with_for_update()).one()
        generation = row.claim_generation
        token = uuid4()
        row.claim_token = token
        row.claimed_until = datetime.now(UTC) + timedelta(seconds=90)
        owner.flush()
        scan = pool.submit(tasks.scan_due_runs, database_engine=engine)
        try:
            # scanner 不能等待后用旧快照清除已续期的领取；被锁的行应跳过。
            scan.result(timeout=2)
        finally:
            owner.commit()
        scan.result(timeout=5)
    with Session(engine) as observer:
        row = observer.get(model, run_id)
        assert row.claim_generation == generation
        assert row.claim_token == token
        assert observer.exec(select(PendingDispatch).where(
            PendingDispatch.tenant_id == seed.context.tenant_id
        )).all() == []
