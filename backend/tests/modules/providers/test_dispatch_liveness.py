"""Real PG scheduler/outbox integration; only clock and external transports vary."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.modules.providers import tasks
from app.modules.providers.models import LinkPreparationItem
from tests.modules.providers.test_preparation_api import prepare
from tests.modules.providers.test_preparation_api import (
    request_fixture as request_fixture,
)
from tests.modules.providers.test_preparation_api import workflow as workflow
from tests.modules.providers.test_preparation_tasks import first_item, make_due


@pytest.fixture
def scheduler(monkeypatch):
    clock = [datetime.now(UTC)]
    messages = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(tasks, "datetime", Clock)
    monkeypatch.setattr(outbox, "datetime", Clock)
    monkeypatch.setattr(
        outbox.celery_app,
        "send_task",
        lambda name, **kwargs: messages.append(kwargs["kwargs"]),
    )
    return clock, messages


def consume(workflow, message):
    tasks.process_item(
        database_engine=engine,
        tenant_id=workflow[0].tenant_id,
        actor_id=workflow[0].actor_id,
        payload=message["payload"],
        transport=httpx.MockTransport(workflow[2]),
    )


def test_batch_outlives_repair_horizon_without_starving_unpublished_work(
    request_fixture, scheduler
):
    clock, messages = scheduler
    task_id = prepare(request_fixture, lines=[f"Absent {n}" for n in range(205)])
    # Every item terminates after one search. The ordinary per-tenant outbox
    # quota needs over three grace horizons to publish this initial batch.
    for tick in range(1, 121):
        clock[0] += timedelta(seconds=5)
        if tick % 3 == 0:
            tasks.recover_preparations(database_engine=engine)
        outbox.flush_dispatch()
        while messages:
            consume(request_fixture, messages.pop(0))
    with Session(engine) as session:
        rows = session.exec(
            select(LinkPreparationItem).where(
                LinkPreparationItem.preparation_id == task_id
            )
        ).all()
        assert len(rows) == 205
        assert all(row.status == "needs_resolution" for row in rows)
        dispatches = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id
            )
        ).all()
        assert len(dispatches) == 410  # Initial delivery plus each unit's watchdog.
    assert len(request_fixture[2].calls) == 205


def test_slow_broker_original_delivery_stays_executable_after_repeated_repair(
    request_fixture, scheduler
):
    clock, messages = scheduler
    task_id = prepare(request_fixture, lines=["Absent"])
    item_id = first_item(task_id)
    clock[0] += timedelta(seconds=5)
    outbox.flush_dispatch()
    # Broker accepts delivery but delays its consumer for many repair horizons.
    for _ in range(5):
        clock[0] += timedelta(seconds=75)
        assert tasks.recover_preparations(database_engine=engine) == 1
        outbox.flush_dispatch()
    with Session(engine) as session:
        records = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id
            )
        ).all()
        assert len(records) == 1
    for message in messages:
        consume(request_fixture, message)
    with Session(engine) as session:
        assert session.get(LinkPreparationItem, item_id).status == "needs_resolution"
    assert len(request_fixture[2].calls) == 1


def test_unpublished_repair_preserves_broker_backoff(request_fixture):
    task_id = prepare(request_fixture)
    make_due(task_id)
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    with Session(engine) as session:
        record = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id
            )
        ).one()
        record.available_at = retry_at
        record.attempts = 8
        session.add(record)
        session.commit()
    tasks.recover_preparations(database_engine=engine)
    with Session(engine) as session:
        records = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == request_fixture[0].tenant_id
            )
        ).all()
        assert len(records) == 1
        assert records[0].available_at == retry_at
        assert records[0].attempts == 8
