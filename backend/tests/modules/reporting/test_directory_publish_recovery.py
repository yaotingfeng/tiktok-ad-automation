"""末页本地发布失败不能永久占用后台窗口，旧代数不得重放。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.ads import tasks
from app.modules.ads.directory_dispatch import enqueue_directory_step
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun
from app.modules.reporting import job_tasks
from tests.modules.ads.test_directory_requests import _route


def _terminal(factory, seed, now, *, lease=None):
    with factory() as session, session.begin():
        run = AdDirectoryRun(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            bc_id="bc-a",
            actor_id=seed.actor_id,
            connection_id=seed.connection.id,
            frozen_route=_route(seed),
            channel="OFFICIAL_API",
            workload="background",
            kind="ad",
            ad_type="SMART_PLUS",
            partition_key="a" * 64,
            query={"ids": [], "parent_ids": [], "include_deleted": True},
            status="RUNNING",
            next_page=2,
            claim_generation=1,
            claimed_until=lease,
        )
        session.add(run)
        session.flush()
        session.add(
            AdDirectoryPage(
                run_id=run.id,
                page=1,
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                complete=True,
                claim_generation=1,
                items=[],
                materials=[],
            )
        )
        enqueue_directory_step(session, run, publish_only=True)
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.publish_step",
            )
        ).one()
        dispatch.published_at = now - timedelta(minutes=32)
        return run.id


@pytest.mark.parametrize("leased", [False, True])
def test_terminal_orphan_recovers_within_directory_cycle_but_not_during_lease(
    committed_sync_seed, monkeypatch, leased
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    run_id = _terminal(
        factory, seed, now, lease=now + timedelta(seconds=60) if leased else None
    )
    with factory() as session, session.begin():
        assert job_tasks._dispatch_directories(session, now) == (0 if leased else 1)
    with factory() as session:
        run = session.get(AdDirectoryRun, run_id)
        assert run.claim_generation == (1 if leased else 2)
        assert run.published_version is None
        if not leased:
            dispatch = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == seed.tenant_id,
                    PendingDispatch.task_key == f"ads.sync_step:{run_id}:2:page:2",
                )
            ).one()
            assert dispatch.task_name == "ads.publish_step"
    if not leased:
        assert (
            tasks.run_directory_step(
                database_engine=engine,
                redis_client=None,
                context=seed.context,
                run_id=run_id,
                claim_generation=1,
                publish_only=True,
            )
            == "FAILED"
        )


@pytest.mark.parametrize(
    "task_name,age_minutes,recovered",
    [
        ("ads.publish_step", 12, False),
        ("ads.publish_step", 32, True),
        ("ads.sync_step", 12, True),
    ],
)
def test_publish_queue_wait_does_not_consume_read_orphan_recovery_budget(
    committed_sync_seed, monkeypatch, task_name, age_minutes, recovered
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    run_id = _terminal(factory, seed, now)
    with factory() as session, session.begin():
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_name == "ads.publish_step",
            )
        ).one()
        dispatch.task_name = task_name
        dispatch.published_at = now - timedelta(minutes=age_minutes)
    with factory() as session, session.begin():
        row = session.get(AdDirectoryRun, run_id)
        assert job_tasks._message_lost(session, row, now) is recovered
        assert job_tasks._dispatch_directories(session, now) == int(recovered)
    with factory() as session:
        assert session.get(AdDirectoryRun, run_id).claim_generation == (
            2 if recovered else 1
        )


def test_publish_transaction_timeout_rolls_back_then_defers_new_generation(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    now = datetime.now(UTC)
    run_id = _terminal(factory, seed, now)
    original = tasks.publish_directory

    def slow(session, **kwargs):
        original(session, **kwargs)
        session.connection().execute(text("SELECT pg_sleep(0.3)"))

    monkeypatch.setattr(tasks, "_PUBLISH_TRANSACTION_TIMEOUT", "150ms")
    monkeypatch.setattr(tasks, "publish_directory", slow)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=None,
            context=seed.context,
            run_id=run_id,
            claim_generation=1,
            publish_only=True,
        )
        == "WAIT"
    )
    with factory() as session:
        run = session.get(AdDirectoryRun, run_id)
        assert run.published_version is None and run.status == "WAITING_REMOTE"
        assert run.error_code == "directory_publish_retry"
        assert run.claim_generation == 2 and run.claimed_until is None
        assert run.next_attempt_at > now
        assert session.get(AdDirectoryPage, (run_id, 1)).complete
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key == f"directory-refill:{run_id}:1",
            )
        ).all()
    monkeypatch.setattr(tasks, "_PUBLISH_TRANSACTION_TIMEOUT", "75s")
    monkeypatch.setattr(tasks, "publish_directory", original)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=None,
            context=seed.context,
            run_id=run_id,
            claim_generation=2,
            publish_only=True,
        )
        == "READY"
    )
    with factory() as session:
        assert session.get(AdDirectoryRun, run_id).status == "COMPLETE"


@pytest.mark.parametrize("changed", ["generation", "lease", "complete"])
def test_publish_failure_recovery_does_not_overwrite_concurrent_progress(
    committed_sync_seed, monkeypatch, changed
):
    seed, factory = committed_sync_seed
    now = datetime.now(UTC)
    run_id = _terminal(factory, seed, now)
    failure = DBAPIError(
        "", {}, Exception("connection lost"), connection_invalidated=True
    )

    def fail_after_progress(**_kwargs):
        # 原连接出错期间另一处理器已经提交；恢复必须锁定后复核当前事实。
        with factory() as session, session.begin():
            run = session.get(AdDirectoryRun, run_id)
            if changed == "generation":
                run.claim_generation = 2
            elif changed == "lease":
                run.claimed_until = now + timedelta(minutes=1)
            else:
                run.status = "COMPLETE"
                run.published_version = 1
        raise failure

    monkeypatch.setattr(tasks, "_run_directory_step", fail_after_progress)
    assert (
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=None,
            context=seed.context,
            run_id=run_id,
            claim_generation=1,
            publish_only=True,
        )
        == "FAILED"
    )
    with factory() as session:
        run = session.get(AdDirectoryRun, run_id)
        assert run.claim_generation == (2 if changed == "generation" else 1)
        assert run.status == ("COMPLETE" if changed == "complete" else "RUNNING")
        assert run.published_version == (1 if changed == "complete" else None)
        assert (run.claimed_until is not None) == (changed == "lease")
        assert run.error_code is None
        assert not session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == seed.tenant_id,
                PendingDispatch.task_key.startswith("directory-refill:"),
            )
        ).all()


@pytest.mark.parametrize("failure_kind", ["domain", "database", "read_step"])
def test_non_retryable_or_remote_phase_failures_are_not_hidden(
    committed_sync_seed, monkeypatch, failure_kind
):
    seed, factory = committed_sync_seed
    run_id = _terminal(factory, seed, datetime.now(UTC))
    failure = (
        DomainError("directory_scope_mismatch", "scope")
        if failure_kind == "domain"
        else DBAPIError(
            "",
            {},
            Exception("failure"),
            connection_invalidated=failure_kind == "read_step",
        )
    )

    def fail(**_kwargs):
        raise failure

    monkeypatch.setattr(tasks, "_run_directory_step", fail)
    with pytest.raises(type(failure)) as raised:
        tasks.run_directory_step(
            database_engine=engine,
            redis_client=None,
            context=seed.context,
            run_id=run_id,
            claim_generation=1,
            publish_only=failure_kind != "read_step",
        )
    assert raised.value is failure
    with factory() as session:
        run = session.get(AdDirectoryRun, run_id)
        assert run.claim_generation == 1 and run.status == "RUNNING"
        assert run.error_code is None
