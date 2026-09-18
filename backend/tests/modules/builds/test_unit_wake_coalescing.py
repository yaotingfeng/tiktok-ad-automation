"""真实 PG 和 Redis 边界：前置完成不能向积压的业务队列重复投递同一单元。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.builds.dispatch import (
    UNIT_TASK,
    process_unit,
    repair_execution,
    wake_unit,
)
from app.modules.builds.execution_models import ExecutionStep, SubmissionUnit
from tests.modules.builds.test_execution import executable as executable


@pytest.mark.parametrize("completions", [90, 1000])
def test_published_unit_coalesces_burst_until_consumer_runs(
    executable,
    redis_client,
    redis_key_prefix,
    monkeypatch,
    record_property,
    completions,
):
    db, context, _ = executable
    queue = f"{redis_key_prefix}:builds"
    monkeypatch.setattr(outbox, "engine", db)

    def publish(name, **kwargs):
        if name == UNIT_TASK:
            redis_client.rpush(queue, kwargs["task_id"])

    # 仅替换 broker 边界；生产 outbox 决定发布时间和ID，业务consumer故意积压。
    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id, delivery, revision = (
            unit.unit_id,
            unit.dispatch_id,
            unit.dispatch_revision,
        )
    started = perf_counter()
    try:
        outbox.flush_dispatch()
        for _ in range(completions):
            with Session(db) as session, session.begin():
                wake_unit(session, unit_id=unit_id, context=context)
            outbox.flush_dispatch()
        messages = redis_client.lrange(queue, 0, -1)
        with Session(db) as session:
            rows = session.exec(
                select(PendingDispatch).where(PendingDispatch.task_name == UNIT_TASK)
            ).all()
            unit = session.exec(select(SubmissionUnit)).one()
            metrics = {
                "completions": completions,
                "broker_messages": len(messages),
                "outbox_rows": len(rows),
                "seconds": round(perf_counter() - started, 3),
            }
            for key, value in metrics.items():
                record_property(key, value)
            assert messages == [str(delivery)], metrics
            assert len(rows) == 1
            assert (unit.dispatch_id, unit.dispatch_revision) == (delivery, revision)
    finally:
        redis_client.delete(queue)


def test_unit_wake_preserves_existing_broker_backoff(executable):
    db, context, _ = executable
    future = datetime.now(UTC) + timedelta(minutes=3)
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id, identity, revision = (
            unit.unit_id,
            unit.dispatch_id,
            unit.dispatch_revision,
        )
        row = session.get(PendingDispatch, identity)
        row.available_at, row.attempts = future, 3
    with Session(db) as session, session.begin():
        wake_unit(session, unit_id=unit_id, context=context)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        row = session.get(PendingDispatch, unit.dispatch_id)
        assert (unit.dispatch_id, unit.dispatch_revision) == (identity, revision)
        assert (row.available_at, row.attempts) == (future, 3)


def test_unit_wake_only_accelerates_future_dependency_rows(executable):
    db, context, ids = executable
    past = datetime.now(UTC) - timedelta(minutes=2)
    future = datetime.now(UTC) + timedelta(minutes=2)
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id = unit.unit_id
        past_step = session.get(ExecutionStep, ids["ADGROUP"][0])
        future_step = session.get(ExecutionStep, ids["AD"][0])
        past_id, future_id = past_step.id, future_step.id
        for step, due_at in [(past_step, past), (future_step, future)]:
            step.status, step.error_code, step.due_at = (
                "PENDING",
                "dependency_pending",
                due_at,
            )
            session.add(step)

    with Session(db) as session, session.begin():
        wake_unit(session, unit_id=unit_id, context=context)
    with Session(db) as session:
        assert session.get(ExecutionStep, past_id).due_at == past
        accelerated = session.get(ExecutionStep, future_id).due_at
        assert past < accelerated < future

    with Session(db) as session, session.begin():
        wake_unit(session, unit_id=unit_id, context=context)
    with Session(db) as session:
        assert session.get(ExecutionStep, past_id).due_at == past
        assert session.get(ExecutionStep, future_id).due_at == accelerated


@pytest.mark.parametrize("changed", ["tenant", "actor", "unit", "revision"])
def test_unit_wake_never_coalesces_different_scope(executable, changed):
    db, context, _ = executable
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id, old = unit.unit_id, unit.dispatch_id
        row = session.get(PendingDispatch, old)
        if changed == "tenant":
            row.tenant_id = uuid4()
        elif changed == "actor":
            row.actor_id = uuid4()
        else:
            row.payload = {
                "unit_id": str(uuid4()) if changed == "unit" else str(unit_id),
                "revision": unit.dispatch_revision + int(changed == "revision"),
            }
    with Session(db) as session, session.begin():
        wake_unit(session, unit_id=unit_id, context=context)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        assert unit.dispatch_id != old
        row = session.get(PendingDispatch, unit.dispatch_id)
        assert (row.tenant_id, row.actor_id, row.payload) == (
            context.tenant_id,
            context.actor_id,
            {"unit_id": str(unit_id), "revision": unit.dispatch_revision},
        )


def test_completion_after_unit_consumed_schedules_next_generation(executable):
    db, context, _ = executable
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        old = unit.dispatch_id
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
        unit_id = unit.unit_id
    process_unit(database_engine=db, context=context, payload=payload)
    with Session(db) as session, session.begin():
        assert session.exec(select(SubmissionUnit)).one().dispatch_id is None
        wake_unit(session, unit_id=unit_id, context=context)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        assert unit.dispatch_id is not None and unit.dispatch_id != old
        current = unit.dispatch_id, unit.dispatch_revision
    assert process_unit(database_engine=db, context=context, payload=payload) == 0
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        assert (unit.dispatch_id, unit.dispatch_revision) == current


def test_lost_published_unit_is_repaired_using_same_delivery(executable):
    db, context, _ = executable
    with Session(db) as session, session.begin():
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id, identity, revision = (
            unit.unit_id,
            unit.dispatch_id,
            unit.dispatch_revision,
        )
        unit.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        session.get(PendingDispatch, identity).published_at = datetime.now(UTC)
    with Session(db) as session, session.begin():
        wake_unit(session, unit_id=unit_id, context=context)
    repair_execution(database_engine=db)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        row = session.get(PendingDispatch, identity)
        assert (unit.dispatch_id, unit.dispatch_revision) == (identity, revision)
        assert row.published_at is None
        assert row.available_at <= datetime.now(UTC)


def test_wake_waiting_for_consumer_lock_schedules_followup(executable):
    db, context, _ = executable
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        unit_id, identity, revision = (
            unit.unit_id,
            unit.dispatch_id,
            unit.dispatch_revision,
        )
    consumer_locked, release_consumer, producer_started = Event(), Event(), Event()
    producer_pid = []

    def pause_consumer(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            statement.startswith("SELECT submission_unit.")
            and "FOR UPDATE" in statement
            and not consumer_locked.is_set()
        ):
            consumer_locked.set()
            assert release_consumer.wait(5)

    def complete_predecessor():
        with Session(db) as session, session.begin():
            producer_pid.append(session.exec(text("SELECT pg_backend_pid()")).one()[0])
            producer_started.set()
            wake_unit(session, unit_id=unit_id, context=context)

    event.listen(db, "after_cursor_execute", pause_consumer)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            consumer = pool.submit(
                process_unit,
                database_engine=db,
                context=context,
                payload={"unit_id": str(unit_id), "revision": revision},
            )
            try:
                assert consumer_locked.wait(5)
                producer = pool.submit(complete_predecessor)
                assert producer_started.wait(5)
                # PostgreSQL 的阻塞关系证明是真实锁竞争，无需用睡眠猜测执行时序。
                with db.connect() as connection:
                    for _ in range(100):
                        blockers = connection.execute(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": producer_pid[0]},
                        ).scalar_one()
                        if blockers:
                            break
                    assert blockers
                assert not producer.done()
            finally:
                release_consumer.set()
            assert consumer.result(timeout=5) > 0
            producer.result(timeout=5)
    finally:
        event.remove(db, "after_cursor_execute", pause_consumer)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        assert unit.dispatch_id != identity
        assert unit.dispatch_revision == revision + 1
        assert session.get(PendingDispatch, unit.dispatch_id).published_at is None
