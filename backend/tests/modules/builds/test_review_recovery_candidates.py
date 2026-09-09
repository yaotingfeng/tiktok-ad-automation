"""Independent small-DB qualification probes for the candidate partition."""

from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

from sqlalchemy import text

from app.jobs.outbox import enqueue_after_commit
from app.modules.builds import recovery
from app.modules.builds.execution_models import ExecutionStep
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_recovery_capacity import expanded
from tests.modules.builds.test_submissions import frozen as frozen


def ids(session, row, *, account=True, **overrides):
    params = {**recovery._params(row), **overrides}
    return list(
        session.execute(
            text("SELECT s.id " + recovery._query(row, "RECONCILE", account=account)),
            params,
        ).scalars()
    )


def test_readback_partition_exhaustive_status_dispatch_lease_matrix(
    session, context, frozen
):
    submission, steps = expanded(session, context, frozen)
    target = next(row for row in steps if row.kind == "READBACK")
    parent = session.get(ExecutionStep, target.parent_step_id)
    dispatch = enqueue_after_commit(
        session,
        context=context,
        task_name="jobs.probe",
        task_key="review-candidate-" + str(uuid4()),
        payload={},
    )
    statuses = (
        "QUEUED",
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "RETRYABLE",
        "UNKNOWN",
    )
    for status, mismatch, known, dispatched, live in product(
        statuses, (False, True), (False, True), (False, True), (False, True)
    ):
        target.status, target.mismatch = status, mismatch
        parent.remote_id = "known-parent" if known else None
        target.dispatch_id = dispatch if dispatched else None
        target.lease_token = uuid4()
        target.lease_expires_at = datetime.now(UTC) + timedelta(
            minutes=10 if live else -10
        )
        session.add_all([target, parent])
        session.flush()
        expected = (
            not dispatched
            and not live
            and (status == "UNKNOWN" or mismatch or (status != "SUCCEEDED" and known))
        )
        actual = ids(session, submission)
        assert actual == ([target.id] if expected else []), (
            status,
            mismatch,
            known,
            dispatched,
            live,
            actual,
        )
        # This check catches UNION ALL duplication where UNKNOWN and known-parent overlap.
        assert len(actual) == len(set(actual))


def test_reconcile_scope_and_armed_known_id_remain_read_only_candidates(
    session, context, frozen
):
    submission, steps = expanded(session, context, frozen)
    target = next(row for row in steps if row.kind == "CTA")
    target.status, target.phase, target.mismatch = "UNKNOWN", "REQUEST_ARMED", True
    target.request_body, target.request_body_digest = {"advertiser_id": "A"}, "a" * 64
    target.remote_id = "receipt-id"
    session.add(target)
    session.flush()
    assert ids(session, submission) == [target.id]
    assert ids(session, submission, account=False) == [target.id]
    assert ids(session, submission, tenant=uuid4()) == []
    assert ids(session, submission, submission=uuid4()) == []
    retry = list(
        session.execute(
            text("SELECT s.id " + recovery._query(submission, "RETRY")),
            recovery._params(submission),
        ).scalars()
    )
    assert retry == []
    # The disjointness relies on booleans and statuses being non-null in real DDL.
    nullability = dict(
        session.execute(
            text(
                "SELECT column_name,is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_step' AND column_name IN ('status','mismatch','kind')"
            )
        ).all()
    )
    assert nullability == {"status": "NO", "mismatch": "NO", "kind": "NO"}
