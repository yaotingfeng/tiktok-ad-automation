"""Real committed tenant rounds within a bounded publisher delivery."""

from sqlmodel import Session, select

from app.core.config import settings
from app.jobs import tasks
from app.jobs.models import PendingDispatch
from tests.jobs.test_expansion_fairness import enqueue
from tests.jobs.test_expansion_fairness import queues as queues
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def test_multiple_rounds_keep_tenant_fairness_and_bound_work(queues, monkeypatch):
    db, first, second, sent = queues
    monkeypatch.setattr(settings, "DISPATCH_MAX_ROUNDS", 3)
    monkeypatch.setattr(settings, "DISPATCH_TIME_BUDGET_SECONDS", 10.0)
    with Session(db) as session, session.begin():
        for _ in range(30):
            enqueue(session, first)
        for _ in range(10):
            enqueue(session, second)
    assert tasks.drain_dispatch() == 25
    tenants = [value[1]["kwargs"]["tenant_id"] for value in sent]
    assert tenants[:10].count(str(first.tenant_id)) == 5
    assert tenants[:10].count(str(second.tenant_id)) == 5
    assert tenants.count(str(first.tenant_id)) == 15
    assert len({value[1]["task_id"] for value in sent}) == 25
    with Session(db) as session:
        assert (
            sum(
                row.published_at is not None
                for row in session.exec(select(PendingDispatch)).all()
            )
            == 25
        )


def test_time_budget_stops_between_committed_rounds(queues, monkeypatch):
    db, first, _, sent = queues
    clock = [0.0]
    monkeypatch.setattr(tasks, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "DISPATCH_MAX_ROUNDS", 20)
    monkeypatch.setattr(settings, "DISPATCH_TIME_BUDGET_SECONDS", 1.0)
    with Session(db) as session, session.begin():
        for _ in range(20):
            enqueue(session, first)

    def publish(name, **kwargs):
        sent.append((name, kwargs))
        clock[0] += 0.3

    monkeypatch.setattr(tasks.celery_app, "send_task", publish)
    assert tasks.drain_dispatch() == 5
    assert len(sent) == 5
    with Session(db) as session:
        assert (
            sum(
                row.published_at is not None
                for row in session.exec(select(PendingDispatch)).all()
            )
            == 5
        )


def test_empty_or_failed_round_does_not_spin_or_reset_backoff(queues, monkeypatch):
    db, first, _, _ = queues
    assert tasks.drain_dispatch() == 0
    with Session(db) as session, session.begin():
        identities = [enqueue(session, first) for _ in range(5)]
    attempts = []

    def broken(*_args, **kwargs):
        attempts.append(kwargs["task_id"])
        raise ConnectionError("synthetic broker unavailable")

    monkeypatch.setattr(tasks.celery_app, "send_task", broken)
    assert tasks.drain_dispatch() == 0
    assert set(attempts) == set(map(str, identities))
    assert len(attempts) == 5
    with Session(db) as session:
        before = {
            row.id: row.available_at
            for row in session.exec(select(PendingDispatch)).all()
        }
        assert all(
            session.get(PendingDispatch, identity).attempts == 1
            for identity in identities
        )
    assert tasks.drain_dispatch() == 0
    with Session(db) as session:
        assert {
            row.id: row.available_at
            for row in session.exec(select(PendingDispatch)).all()
        } == before


def test_production_control_task_uses_bounded_drain(monkeypatch):
    called = []
    monkeypatch.setattr(
        tasks, "drain_dispatch", lambda limit: called.append(limit) or 12
    )
    assert tasks.flush_dispatch_task.run(limit=7) == 12
    assert called == [7]
