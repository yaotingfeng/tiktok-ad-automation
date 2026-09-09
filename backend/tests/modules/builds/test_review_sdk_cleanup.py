"""Independent regressions for the pending SDK cleanup/interrupt patch."""

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text
from sqlmodel import Session, select

from app.integrations.tiktok.sdk import official_client
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_execution import run
from tests.modules.builds.test_review_execution import success_wire


@pytest.mark.parametrize("location", ["clear", "join"])
def test_review_cleanup_soft_limit_is_deferred_until_join_then_propagates(
    monkeypatch, location
):
    """Shield cleanup, but do not silently turn cancellation into success."""
    clients = []
    interrupt = SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        with official_client(access_token="fixture-token") as client:
            clients.append(client)
            if location == "clear":
                original = client.rest_client.pool_manager.clear

                def clear():
                    original()
                    raise interrupt

                monkeypatch.setattr(client.rest_client.pool_manager, "clear", clear)
            else:
                original = client.pool.join
                interrupted = False

                def join():
                    nonlocal interrupted
                    original()
                    if not interrupted:
                        interrupted = True
                        raise interrupt

                monkeypatch.setattr(client.pool, "join", join)
    assert caught.value is interrupt
    assert "Access-Token" not in clients[0].default_headers
    assert all(not thread.is_alive() for thread in clients[0].pool._pool)


@pytest.mark.parametrize("reject_receipt", [False, True])
def test_review_received_cta_id_survives_receipt_commit_and_fatal_cleanup(
    executable, redis_client, monkeypatch, reject_receipt
):
    """Real deferred PG commit failure plus real SDK thread-pool cleanup failure."""
    from multiprocessing.pool import ThreadPool

    db, _, ids = executable
    if reject_receipt:
        with db.begin() as connection:
            connection.execute(
                text("""
                CREATE FUNCTION review_cleanup_reject_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.kind='CTA' AND NEW.status='SUCCEEDED' THEN
                    RAISE EXCEPTION 'review receipt commit rejection' USING ERRCODE='23514';
                  END IF;
                  RETURN NEW;
                END $$;
                CREATE CONSTRAINT TRIGGER review_cleanup_deferred_receipt
                AFTER UPDATE ON execution_step DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION review_cleanup_reject_receipt();
            """)
            )
    calls = success_wire(monkeypatch)
    original_join, joined = ThreadPool.join, set()

    def cleanup_failure(pool):
        original_join(pool)
        if id(pool) not in joined:
            joined.add(id(pool))
            raise RuntimeError("review SDK pool cleanup failed")

    monkeypatch.setattr(ThreadPool, "join", cleanup_failure)
    with pytest.raises(SystemExit):
        run(executable, redis_client, "CTA")
    assert len(calls) == 1
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        evidence = session.exec(
            select(StepEvidence).where(StepEvidence.step_id == step.id)
        ).all()
        assert step.request_body is not None
        assert any(row.summary.get("remote_id") == calls[0][2] for row in evidence), (
            "a received CTA ID must remain recoverable even if its first PG receipt commit fails and SDK cleanup then terminates the process"
        )
        if reject_receipt:
            assert step.status == "UNKNOWN" and step.remote_id is None
        else:
            assert step.status == "SUCCEEDED" and step.remote_id == calls[0][2]
