"""已知本地读取期限退出归还旧资格，不改后继领取或已发送成本。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import CallEvidence, DirectoryPage
from app.modules.ads import tasks
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.sync_control.models import SyncCostLedger, SyncSchedule
from tests.modules.ads.test_directory_quantum import committed_background  # noqa: F401
from tests.modules.reporting.sync_conftest import committed_sync_seed  # noqa: F401


def _page(number):
    return DirectoryPage(
        items=(),
        materials=(),
        page=number,
        complete=False,
        next_page=number + 1,
        evidence=CallEvidence(request_id="local-deadline-test"),
    )


def _reader(monkeypatch, read):
    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_primary_page=read))

    monkeypatch.setattr(tasks, "open_tiktok_gateway", gateway)


def _head(db, tenant):
    return db.exec(select(SyncSchedule).where(SyncSchedule.tenant_id == tenant)).one()


def _snapshot(run, head):
    return (
        run.status,
        run.error_code,
        run.claim_generation,
        run.owner_epoch,
        run.claim_token,
        run.claimed_until,
        run.next_page,
        run.next_attempt_at,
        head.lease_token,
        head.leased_until,
        head.lease_execution_id,
        head.lease_execution_generation,
        head.next_eligible_at,
    )


@pytest.mark.parametrize(
    "code",
    [
        "tiktok_call_deadline_exceeded",
        "read_deadline_exceeded",
        "tiktok_local_resources_unavailable",
    ],
)
def test_known_read_exit_releases_claim_preserves_committed_page_and_cost(
    committed_background,  # noqa: F811
    monkeypatch,
    code,
):
    from app.integrations.tiktok.attempt_observer import physical_attempt_scope
    from app.integrations.tiktok.transport_diagnostics import install_transport_tracking
    from app.modules.sync_control.directory_demand import directory_endpoint
    from app.modules.sync_control.directory_dispatch import dispatch_directories_due
    from app.modules.sync_control.worker_pool import background_lease_count

    seed, factory, args = committed_background
    seen, delivered = [], []
    monkeypatch.setattr(
        tasks,
        "_deliver_committed_work",
        lambda _engine, work: delivered.append(work.refill),
    )
    with factory() as db:
        endpoint = directory_endpoint(db.get(AdDirectoryRun, args["run_id"]))

    class Connection:
        def request(self):
            return "sent"

    class Pool:
        ConnectionCls = Connection

    client = SimpleNamespace(
        rest_client=SimpleNamespace(
            pool_manager=SimpleNamespace(pool_classes_by_scheme={"https": Pool})
        )
    )
    install_transport_tracking(client)
    connection = client.rest_client.pool_manager.pool_classes_by_scheme[
        "https"
    ].ConnectionCls()
    charged = []

    def read(query):
        seen.append(query.page)
        # 第二页已真实发送才发现剩余期限不足，不能把该次改成NOT_SENT或退款。
        with physical_attempt_scope(client, endpoint):
            connection.request()
        if query.page == 2:
            with factory() as db:
                assert db.get(AdDirectoryPage, (args["run_id"], 1)) is not None
                ledger = db.exec(
                    select(SyncCostLedger).where(
                        SyncCostLedger.directory_run_id == args["run_id"]
                    )
                ).one()
                charged.append(ledger.charged_units)
            raise DomainError(
                code,
                "local read stopped",
                retryable=code == "tiktok_local_resources_unavailable",
            )
        return _page(query.page)

    _reader(monkeypatch, read)
    before = datetime.now(UTC)
    assert tasks.run_directory_step(**args) == "WAIT"
    after = datetime.now(UTC)
    assert seen == [1, 2] and delivered == [True]
    with factory() as db:
        run = db.get(AdDirectoryRun, args["run_id"])
        head = _head(db, seed.tenant_id)
        assert run.status == "WAITING_REMOTE" and run.error_code == code
        assert run.claim_generation == args["claim_generation"] + 1
        assert run.claim_token is None and run.claimed_until is None
        assert run.next_page == 2 and run.published_version is None
        assert (
            before + timedelta(seconds=1)
            <= run.next_attempt_at
            <= after + timedelta(seconds=1)
        )
        assert head.active and head.lease_token is None and head.leased_until is None
        assert head.next_eligible_at == run.next_attempt_at
        assert db.get(AdDirectoryPage, (run.id, 1)) is not None
        assert db.get(AdDirectoryPage, (run.id, 2)) is None
        ledger = db.exec(
            select(SyncCostLedger).where(SyncCostLedger.directory_run_id == run.id)
        ).one()
        assert ledger.charged_units == charged[0] == 5
        assert ledger.observed_units == 2
        assert background_lease_count(db, now=after) == 0
        due = run.next_attempt_at
        assert (
            dispatch_directories_due(
                db, args["redis_client"], now=due - timedelta(microseconds=1), limit=1
            )
            == 0
        )
        assert (
            dispatch_directories_due(
                db, args["redis_client"], now=due + timedelta(seconds=1), limit=1
            )
            == 1
        )
        db.refresh(run)
        new_generation = run.claim_generation
        db.commit()
    # 旧消息仍不能再发HTTP；恢复由正常dispatcher重新签发代数。
    assert tasks.run_directory_step(**args) == "FAILED"
    assert seen == [1, 2]

    def resumed(query):
        seen.append(query.page)
        return _page(query.page)

    _reader(monkeypatch, resumed)
    assert (
        tasks.run_directory_step(**(args | {"claim_generation": new_generation}))
        == "CONTINUE"
    )
    assert seen == [1, 2, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("change", ["generation", "owner", "token", "head"])
def test_deadline_exit_does_not_mutate_successor_fence(
    committed_background,  # noqa: F811
    monkeypatch,
    change,
):
    seed, factory, args = committed_background
    expected, delivered = [], []
    monkeypatch.setattr(
        tasks,
        "_deliver_committed_work",
        lambda _engine, work: delivered.append(work.refill),
    )

    def read(_query):
        # HTTP期间另一真实连接改变领取；head情况故意保持父fence完全不变。
        with factory() as db:
            run = db.get(AdDirectoryRun, args["run_id"])
            head = _head(db, seed.tenant_id)
            if change == "generation":
                run.claim_generation += 1
            elif change == "owner":
                run.owner_epoch += 1
            elif change == "token":
                run.claim_token = uuid4()
            else:
                head.lease_token = uuid4()
                head.leased_until += timedelta(seconds=30)
            db.commit()
            expected.append(_snapshot(run, head))
        raise DomainError("read_deadline_exceeded", "local read stopped")

    _reader(monkeypatch, read)
    assert tasks.run_directory_step(**args) == "WAIT"
    with factory() as db:
        assert (
            _snapshot(db.get(AdDirectoryRun, args["run_id"]), _head(db, seed.tenant_id))
            == expected[0]
        )
    assert delivered == [False]


@pytest.mark.parametrize(
    "code,retryable",
    [
        ("read_deadline_invalid", False),
        ("tiktok_response_error", False),
        ("directory_scope_mismatch", False),
        ("tiktok_local_resources_unavailable", False),
    ],
)
def test_other_domain_errors_keep_original_failure_and_lease(
    committed_background,  # noqa: F811
    monkeypatch,
    code,
    retryable,
):
    seed, factory, args = committed_background
    expected = []
    monkeypatch.setattr(tasks, "_deliver_committed_work", lambda *_: None)
    error = DomainError(code, "not a transient read exit", retryable=retryable)

    def read(_query):
        with factory() as db:
            expected.append(
                _snapshot(
                    db.get(AdDirectoryRun, args["run_id"]), _head(db, seed.tenant_id)
                )
            )
        raise error

    _reader(monkeypatch, read)
    with pytest.raises(DomainError) as caught:
        tasks.run_directory_step(**args)
    assert caught.value is error
    with factory() as db:
        assert (
            _snapshot(db.get(AdDirectoryRun, args["run_id"]), _head(db, seed.tenant_id))
            == expected[0]
        )


@pytest.mark.parametrize("publish_only,has_fence", [(True, True), (False, False)])
def test_read_recovery_requires_read_mode_and_committed_fence(
    monkeypatch, publish_only, has_fence
):
    error = DomainError("read_deadline_exceeded", "not a claimed read")

    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(tasks, "_run_directory_step", fail)
    work = tasks._CommittedDirectoryWork()
    if has_fence:
        work.failure_fence = (1, 1, uuid4())
    with pytest.raises(DomainError) as caught:
        tasks._execute_directory_step(
            database_engine=None,
            redis_client=None,
            context=None,
            run_id=uuid4(),
            claim_generation=1,
            publish_only=publish_only,
            work=work,
        )
    assert caught.value is error


@pytest.mark.parametrize("locked", ["parent", "head"])
def test_read_recovery_bounds_contended_lock_without_changing_fences(
    committed_background,  # noqa: F811
    monkeypatch,
    locked,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    from sqlalchemy.exc import DBAPIError

    seed, factory, args = committed_background
    acquired, release = Event(), Event()
    expected, delivered, started = [], [], []
    monkeypatch.setattr(
        tasks,
        "_deliver_committed_work",
        lambda _engine, work: delivered.append(work.refill),
    )

    def hold():
        with factory() as db, db.begin():
            model = AdDirectoryRun if locked == "parent" else SyncSchedule
            statement = select(model).where(
                AdDirectoryRun.id == args["run_id"]
                if locked == "parent"
                else SyncSchedule.tenant_id == seed.tenant_id
            )
            db.exec(statement.with_for_update()).one()
            acquired.set()
            # 有限持锁：旧20秒锁预算会等待到释放后错误地完成清理。
            release.wait(4)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holders = []

        def read(_query):
            with factory() as db:
                expected.append(
                    _snapshot(
                        db.get(AdDirectoryRun, args["run_id"]),
                        _head(db, seed.tenant_id),
                    )
                )
            holders.append(executor.submit(hold))
            assert acquired.wait(2)
            started.append(monotonic())
            raise DomainError("read_deadline_exceeded", "read ended before recovery")

        _reader(monkeypatch, read)
        try:
            if locked == "parent":
                assert tasks.run_directory_step(**args) == "WAIT"
                assert monotonic() - started[0] < 2
            else:
                with pytest.raises(DBAPIError) as caught:
                    tasks.run_directory_step(**args)
                # PostgreSQL事务期限断连；不能把未提交清理伪装成成功WAIT。
                assert caught.value.connection_invalidated
                assert monotonic() - started[0] < 4
        finally:
            release.set()
            for holder in holders:
                holder.result(timeout=2)
    with factory() as db:
        assert (
            _snapshot(db.get(AdDirectoryRun, args["run_id"]), _head(db, seed.tenant_id))
            == expected[0]
        )
    assert delivered == [False]
