"""Sparse recovery candidates preserve the old qualified ID set."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import recovery, submissions
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.routes import save_attempt_context
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen


def expanded(session, context, frozen):
    identity = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    ).submission_id
    while not submissions.expand_submission(
        session, context=context, submission_id=identity
    ):
        pass
    return session.get(Submission, identity), list(
        session.exec(
            select(ExecutionStep)
            .where(ExecutionStep.submission_id == identity)
            .order_by(ExecutionStep.step_key)
        ).all()
    )


@pytest.mark.parametrize(
    "case",
    [
        "pending",
        "unknown",
        "mismatch",
        "succeeded_mismatch",
        "account_denied",
        "readback_known",
        "readback_overlap",
        "readback_succeeded",
        "retry",
        "armed_evidence",
        "active_lease",
        "material_failed",
    ],
)
def test_recovery_candidates_equal_legacy(session, context, frozen, case):
    row, steps = expanded(session, context, frozen)
    cta = next(s for s in steps if s.kind == "CTA")
    readbacks = [s for s in steps if s.kind == "READBACK"]
    if case == "unknown":
        for s in steps:
            s.status = "UNKNOWN"
    elif case in {"mismatch", "succeeded_mismatch"}:
        for s in steps:
            s.mismatch = True
            if case == "succeeded_mismatch":
                s.status = "SUCCEEDED"
    elif case.startswith("readback_"):
        for s in readbacks:
            session.get(ExecutionStep, s.parent_step_id).remote_id = "remote-" + str(
                s.id
            )
            if case == "readback_overlap":
                s.status, s.mismatch = "UNKNOWN", True
            if case == "readback_succeeded":
                s.status = "SUCCEEDED"
    elif case in {"retry", "armed_evidence", "active_lease"}:
        cta.status = "FAILED"
        if case == "armed_evidence":
            save_attempt_context(session, step=cta, attempt=1)
            session.add(
                StepEvidence(
                    tenant_id=row.tenant_id,
                    submission_id=row.id,
                    step_id=cta.id,
                    attempt=1,
                    conclusion="LATE_CREATED",
                    summary={"remote_id": "known"},
                )
            )
        if case == "active_lease":
            cta.lease_token, cta.lease_expires_at = (
                uuid4(),
                datetime.now(UTC) + timedelta(minutes=3),
            )
    elif case == "material_failed":
        for s in steps:
            if s.kind == "MATERIAL":
                s.status = "FAILED"
    if case == "account_denied":
        cta.status = "FAILED"
        for access in session.exec(
            select(BCAccountAccess).where(BCAccountAccess.tenant_id == row.tenant_id)
        ):
            access.active = False
            session.add(access)
    for s in steps:
        session.add(s)
    session.flush()
    params = recovery._params(row)
    for kind in ["RETRY", "RECONCILE"]:
        legacy = (
            Path(__file__)
            .with_name("legacy_recovery_" + kind.lower() + ".sql")
            .read_text()
        )
        for account in (True, False):
            oracle = legacy if account else legacy.rsplit("AND u.connection_id=", 1)[0]
            expected = set(session.execute(text(oracle), params).scalars())
            actual = list(
                session.execute(
                    text("SELECT s.id " + recovery._query(row, kind, account=account)),
                    params,
                ).scalars()
            )
            assert len(actual) == len(set(actual)), (
                "Overlapping candidate branches must not double count"
            )
            assert set(actual) == expected
            if case in {
                "pending",
                "readback_succeeded",
                "armed_evidence",
                "active_lease",
            }:
                assert not actual
            if case in {"readback_known", "readback_overlap"} and kind == "RECONCILE":
                assert len(actual) == len(readbacks)
            if case == "account_denied" and kind == "RETRY":
                assert len(actual) == (0 if account else 1)
            assert not list(
                session.execute(
                    text("SELECT s.id " + recovery._query(row, kind, account=account)),
                    {**params, "tenant": uuid4()},
                ).scalars()
            )
    if case == "account_denied":
        summary = recovery.recovery_summary(session, context=context, submission=row)
        assert summary.reasons == ["account_access_denied"]
        assert not summary.can_retry and not summary.can_reconcile


def nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from nodes(child)


def test_generic_reconciliation_plan_uses_sparse_candidates_and_known_parent_indexes(
    session, context, frozen
):
    row, _ = expanded(session, context, frozen)
    # Exercise literal partial-index implication with a prepared generic plan.
    # Avoid a planner preference for tiny-fixture sequential scans.
    session.execute(text("SET LOCAL enable_seqscan=off"))
    session.execute(text("SET LOCAL plan_cache_mode='force_generic_plan'"))
    sql = "SELECT count(*) " + recovery._query(row, "RECONCILE")
    sql = (
        sql.replace(":tenant", "$1")
        .replace(":submission", "$2")
        .replace(":now", "$3")
        .replace(":cutoff", "$4")
    )
    session.execute(
        text("PREPARE recovery_capacity(uuid,uuid,timestamptz,timestamptz) AS " + sql)
    )
    params = recovery._params(row)
    try:
        plan = session.execute(
            text(
                f"EXPLAIN (FORMAT JSON) EXECUTE recovery_capacity('{row.tenant_id}','{row.id}','{params['now'].isoformat()}','{params['cutoff'].isoformat()}')"
            )
        ).scalar_one()[0]["Plan"]
        names = {n.get("Index Name") for n in nodes(plan)}
        assert {
            "ix_recovery_candidates",
            "ix_recovery_known_parent",
            "ix_recovery_readback_parent",
        } <= names
        assert not any(
            n.get("Relation Name") == "execution_step"
            and n.get("Node Type") == "Seq Scan"
            for n in nodes(plan)
        )
    finally:
        session.execute(text("DEALLOCATE recovery_capacity"))
