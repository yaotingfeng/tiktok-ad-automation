"""真实 PostgreSQL 验证取消与入口并发，取消后仅恢复未知请求证据。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ad_management import tasks
from app.modules.ad_management.actions import cancel_task
from app.modules.ad_management.direct_submission import submit_direct_task
from app.modules.ad_management.models import (
    ManagementReceipt,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ad_management.schemas import MutationSpec
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _row, _set_selection
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


@pytest.fixture
def queued_task(committed_management_env):
    context, bc, account, route, selection = committed_management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "cancel-race")
    with Session(engine) as session:
        row = session.get(FrozenSelectionRecord, selection.id)
        _set_selection(row, [ref])
        session.add(row)
        session.add(_row(ref, connection_id=route.connection_id))
        session.flush()
        task_id = submit_direct_task(
            session,
            context,
            bc.bc_id,
            selection.id,
            MutationSpec(field="status", mode="set", value="DISABLE"),
            uuid4(),
        ).task_id
        session.commit()
    prepare_direct_task(engine, task_id)
    with Session(engine) as session:
        item = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task_id)
        ).one()
        item_id = item.id
    return context, task_id, item_id


def _run(context, task_id):
    return tasks.execute_management_task.run(
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        payload={"task_id": str(task_id), "generation": 1},
    )


def _claim_then_cancel(context, task_id, item_id, *, expired):
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        item.claim_token = uuid4()
        item.claimed_until = datetime.now(UTC) + timedelta(
            seconds=-1 if expired else 120
        )
        item.delivery_status = "ATTEMPT_RECORDED"
        attempt = ManagementRequestAttempt(
            tenant_id=context.tenant_id,
            task_item_id=item_id,
            outcome="NOT_SENT",
            retryable=False,
        )
        session.add_all([item, attempt])
        session.commit()
        cancel_task(session, context, task_id)
        session.commit()
        return attempt.id


@pytest.mark.parametrize("expired", [True, False])
def test_cancelled_task_recovers_expired_claim_without_apply(
    queued_task, monkeypatch, expired
):
    context, task_id, item_id = queued_task
    attempt_id = _claim_then_cancel(context, task_id, item_id, expired=expired)

    def no_dispatch(*_args, **_kwargs):
        pytest.fail("取消任务不得进入写请求派发")

    monkeypatch.setattr(
        "app.modules.ad_management.dispatch_work.execute_task_items", no_dispatch
    )
    assert _run(context, task_id) == "CANCELLED"
    if not expired:
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item.execution_result == "PENDING" and item.claim_token is not None
            wakeups = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.task_key.startswith(
                        f"ad-management-wake:{task_id}:claim:"
                    ),
                )
            ).all()
            assert any(row.available_at > item.claimed_until for row in wakeups)
            item.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
            session.add(item)
            session.commit()
        assert _run(context, task_id) == "CANCELLED"
    assert _run(context, task_id) == "CANCELLED"
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "UNKNOWN"
        assert item.observation_state == "RECONCILE_REQUESTED"
        assert item.claim_token is None and item.claimed_until is None
        assert session.get(ManagementRequestAttempt, attempt_id).outcome == "UNKNOWN"
        receipts = session.exec(
            select(ManagementReceipt).where(
                ManagementReceipt.attempt_id == attempt_id,
            )
        ).all()
        assert (
            len(
                [
                    r
                    for r in receipts
                    if r.response.get("reason") == "attempt_claim_expired"
                ]
            )
            == 1
        )
        reads = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "ad_management.reconcile",
            )
        ).all()
        assert len(reads) == 1
        assert session.get(ManagementTask, task_id).counts["unknown"] == 1


def test_worker_entry_serializes_with_cancel_before_status_write(
    queued_task, monkeypatch
):
    context, task_id, _ = queued_task
    entry_read, release_entry, cancel_finished = Event(), Event(), Event()
    cancel_pid = []
    original = tasks._queue_unknown_observations

    def pause_after_read(session, task):
        if not entry_read.is_set():
            entry_read.set()
            assert release_entry.wait(10)
        return original(session, task)

    def cancelled(*_args):
        assert cancel_finished.wait(10)
        return True

    monkeypatch.setattr(tasks, "_queue_unknown_observations", pause_after_read)
    monkeypatch.setattr(
        "app.modules.ad_management.dispatch_work.execute_task_items", cancelled
    )

    def cancel():
        with Session(engine) as session:
            cancel_pid.append(session.exec(text("select pg_backend_pid()")).scalar())
            cancel_task(session, context, task_id)
            session.commit()
        cancel_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run, context, task_id)
        assert entry_read.wait(10)
        cancellation = pool.submit(cancel)
        try:
            deadline = monotonic() + 10
            while not cancel_finished.is_set():
                if cancel_pid:
                    with engine.connect() as connection:
                        blocked = connection.execute(
                            text(
                                "select wait_event_type = 'Lock' from pg_stat_activity where pid = :pid"
                            ),
                            {"pid": cancel_pid[0]},
                        ).scalar()
                    if blocked:
                        break
                assert monotonic() < deadline
                sleep(0.01)
        finally:
            release_entry.set()
        cancellation.result(timeout=10)
        assert worker.result(timeout=10) == "CANCELLED"
    with Session(engine) as session:
        assert session.get(ManagementTask, task_id).status == "CANCELLED"


def test_redis_failure_uses_broker_retry(queued_task, monkeypatch):
    from celery.exceptions import Retry
    from redis.exceptions import ConnectionError, RedisError

    context, task_id, _ = queued_task
    assert RedisError in tasks.execute_management_task.autoretry_for
    retries = []

    def unavailable(*_args):
        raise ConnectionError("synthetic unavailable coordination")

    def retry(**kwargs):
        retries.append(kwargs)
        raise Retry()

    monkeypatch.setattr(
        "app.modules.ad_management.dispatch_work.execute_task_items", unavailable
    )
    monkeypatch.setattr(tasks.execute_management_task, "retry", retry)
    with pytest.raises(Retry):
        _run(context, task_id)
    assert len(retries) == 1 and isinstance(retries[0]["exc"], ConnectionError)


def test_cancelled_single_item_delivery_only_recovers_receipt(queued_task, monkeypatch):
    from app.modules.ad_management.execution import execute_item

    context, task_id, item_id = queued_task
    _claim_then_cancel(context, task_id, item_id, expired=True)

    def no_gateway(**_kwargs):
        pytest.fail("取消后的单项投递不得打开平台写网关")

    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway", no_gateway
    )
    execute_item(engine, item_id)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "UNKNOWN"
        assert item.observation_state == "RECONCILE_REQUESTED"
        assert session.get(ManagementTask, task_id).status == "CANCELLED"
