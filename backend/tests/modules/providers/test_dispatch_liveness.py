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


def test_repair_cannot_rearm_live_send_or_change_its_attempt_fence(request_fixture):
    from app.modules.providers.models import ProviderEffect
    from tests.modules.providers.test_preparation_tasks import process

    task_id = prepare(request_fixture)
    item_id = first_item(task_id)
    process(request_fixture, item_id)  # Search.
    process(request_fixture, item_id)  # Complete channel lookup.
    observed_attempt = []

    def during_create(request):
        assert request.url.path.endswith("/create")
        with Session(engine) as observer:
            # The actual provider transport can acquire both rows immediately;
            # no workflow transaction or row lock spans this HTTP request.
            item = observer.exec(
                select(LinkPreparationItem)
                .where(
                    LinkPreparationItem.id == item_id,
                )
                .with_for_update(nowait=True)
            ).one()
            effect = observer.exec(
                select(ProviderEffect)
                .where(
                    ProviderEffect.tenant_id == request_fixture[0].tenant_id,
                    ProviderEffect.step == "create",
                )
                .with_for_update(nowait=True)
            ).one()
            work = dict(item.resolved["_work"])
            revision, claim = work["dispatch_revision"], work["claim_token"]
            assert effect.status == "sending" and str(effect.attempt_token) == claim
            observed_attempt.append(effect.attempt_token)
            work["repair_after"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
            item.resolved = {**item.resolved, "_work": work}
            observer.add(item)
            dispatch = observer.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == item.tenant_id,
                    PendingDispatch.task_key == f"provider-item:{item.id}:{revision}",
                )
            ).one()
            published_at = datetime.now(UTC)
            dispatch.published_at = published_at
            observer.add(dispatch)
            dispatch_id = dispatch.id
            observer.commit()
        # Even a forced due horizon cannot re-arm a valid live item claim.
        assert tasks.recover_preparations(database_engine=engine) == 0
        with Session(engine) as observer:
            current = observer.get(LinkPreparationItem, item_id).resolved["_work"]
            dispatch = observer.get(PendingDispatch, dispatch_id)
            assert (
                current["dispatch_revision"] == revision
                and current["claim_token"] == claim
            )
            assert dispatch.published_at == published_at
            effect = observer.exec(
                select(ProviderEffect).where(
                    ProviderEffect.tenant_id == request_fixture[0].tenant_id,
                    ProviderEffect.step == "create",
                )
            ).one()
            assert (
                effect.status == "sending"
                and effect.attempt_token == observed_attempt[0]
            )
        return request_fixture[2](request)

    process(request_fixture, item_id, transport=httpx.MockTransport(during_create))
    with Session(engine) as session:
        effect = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == request_fixture[0].tenant_id,
                ProviderEffect.step == "create",
            )
        ).one()
        assert (
            effect.status == "succeeded" and effect.attempt_token == observed_attempt[0]
        )
    assert request_fixture[2].calls.count("create") == 1
