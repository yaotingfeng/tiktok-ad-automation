"""Priority is a bounded queue category, never a forged availability timestamp."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.jobs import outbox
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.builds.submission_tasks import TASK_NAME, UNIT_TASK_NAME
from tests.modules.conftest import create_context
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)


@pytest.fixture
def queues(isolated_strategy_database, monkeypatch):
    db, first, _ = isolated_strategy_database
    with Session(db) as session, session.begin():
        second = create_context(session)
    sent = []
    monkeypatch.setattr(outbox, "engine", db)
    monkeypatch.setattr(
        outbox.celery_app, "send_task", lambda name, **kw: sent.append((name, kw))
    )
    return db, first, second, sent


def enqueue(session, context, *, expansion=False, available=None):
    identity = uuid4()
    key = "submission_id" if expansion else "unit_id"
    record_id = enqueue_after_commit(
        session,
        context=context,
        task_name=TASK_NAME if expansion else UNIT_TASK_NAME,
        task_key=str(identity),
        payload={key: str(identity), "revision": 0},
    )
    row = session.get(PendingDispatch, record_id)
    row.available_at = available or datetime.now(UTC)
    session.add(row)
    return record_id


def test_expansion_is_published_next_round_despite_200_older_units(queues):
    db, first, second, sent = queues
    old = datetime.now(UTC) - timedelta(minutes=1)
    with Session(db) as session, session.begin():
        for _ in range(200):
            enqueue(session, first, available=old)
        successor = enqueue(session, first, expansion=True)
        other = enqueue(session, second)
    assert outbox.flush_dispatch(limit=10) == 6
    ids = [row[1]["task_id"] for row in sent]
    assert str(successor) in ids, "expansion must not wait 40 ordinary broker rounds"
    assert str(other) in ids
    assert sum(row[0] == TASK_NAME for row in sent) == 1
    with Session(db) as session:
        row = session.get(PendingDispatch, successor)
        assert row.available_at > old
        assert row.attempts == 1 and row.published_at


def test_multiple_submissions_cannot_consume_ordinary_slots(queues):
    db, first, _, sent = queues
    with Session(db) as session, session.begin():
        expansions = [enqueue(session, first, expansion=True) for _ in range(12)]
        ordinary = [enqueue(session, first) for _ in range(12)]
    for count in range(3):
        assert outbox.flush_dispatch() == 5
        recent = sent[count * 5 :]
        assert sum(name == TASK_NAME for name, _ in recent) == 1
        assert sum(name == UNIT_TASK_NAME for name, _ in recent) == 4
    actual = [row[1]["task_id"] for row in sent]
    assert set(map(str, ordinary)) <= set(actual)
    assert len(set(map(str, expansions)) & set(actual)) == 3
    assert len(set(actual)) == len(actual)
    assert [kw["task_id"] for name, kw in sent if name == UNIT_TASK_NAME] == list(
        map(str, ordinary)
    )


def test_expansion_backoff_is_respected_while_other_tenants_and_units_publish(
    queues, monkeypatch
):
    db, first, second, sent = queues
    future = datetime.now(UTC) + timedelta(minutes=3)
    with Session(db) as session, session.begin():
        delayed = enqueue(session, first, expansion=True, available=future)
        failing = enqueue(session, first, expansion=True)
        for _ in range(8):
            enqueue(session, first)
        other = enqueue(session, second, expansion=True)

    def publish(name, **kwargs):
        if kwargs["task_id"] == str(failing):
            raise ConnectionError("simulated broker loss")
        sent.append((name, kwargs))

    monkeypatch.setattr(outbox.celery_app, "send_task", publish)
    assert outbox.flush_dispatch(limit=10) == 5
    assert str(other) in [row[1]["task_id"] for row in sent]
    with Session(db) as session:
        broken = session.get(PendingDispatch, failing)
        retry_at = broken.available_at
        assert broken.published_at is None and broken.attempts == 1
        assert retry_at > datetime.now(UTC)
    assert outbox.flush_dispatch(limit=10) == 4
    with Session(db) as session:
        rows = [
            session.get(PendingDispatch, identity) for identity in (delayed, failing)
        ]
        assert [(r.available_at, r.attempts, r.published_at) for r in rows] == [
            (future, 0, None),
            (retry_at, 1, None),
        ]
    assert outbox.flush_dispatch(limit=10) == 0
