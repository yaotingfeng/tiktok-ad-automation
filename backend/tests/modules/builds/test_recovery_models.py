"""Recovery intent is tenant scoped and permanent independently of job progress."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session

from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.recovery_models import (
    SubmissionRecovery,
    SubmissionRecoveryRequest,
)
from tests.modules.builds.test_execution import executable as executable


def insert_job(session, context, submission):
    job = SubmissionRecovery(
        tenant_id=context.tenant_id,
        submission_id=submission.id,
        preview_id=submission.preview_id,
        bc_id=submission.bc_id,
        request_id=uuid4(),
        request_actor_id=context.actor_id,
        kind="RETRY",
    )
    session.add(job)
    session.flush()
    session.add(
        SubmissionRecoveryRequest(
            tenant_id=context.tenant_id,
            request_id=job.request_id,
            recovery_id=job.id,
            submission_id=submission.id,
            bc_id=submission.bc_id,
            kind="RETRY",
        )
    )
    session.flush()
    return job


def test_recovery_intent_and_receipt_are_immutable_but_progress_can_change(executable):
    db, context, ids = executable
    with Session(db) as session:
        submission = session.get(
            Submission, session.get(ExecutionStep, ids["CTA"][0]).submission_id
        )
        job = insert_job(session, context, submission)
        job.state, job.scanned_count, job.scheduled_count = "COMPLETED", 3, 2
        session.flush()
        assert job.scheduled_count == 2
        for sql in [
            "UPDATE submission_recovery SET kind='RECONCILE' WHERE id=:id",
            "DELETE FROM submission_recovery WHERE id=:id",
            "UPDATE submission_recovery_request SET kind='RECONCILE' WHERE recovery_id=:id",
            "DELETE FROM submission_recovery_request WHERE recovery_id=:id",
        ]:
            with pytest.raises(DBAPIError), session.begin_nested():
                session.execute(text(sql), {"id": job.id})


@pytest.mark.parametrize("field", ["tenant_id", "preview_id", "bc_id"])
def test_recovery_cannot_cross_frozen_scope(executable, field):
    db, context, ids = executable
    with Session(db) as session:
        submission = session.get(
            Submission, session.get(ExecutionStep, ids["CTA"][0]).submission_id
        )
        values = {
            "tenant_id": context.tenant_id,
            "submission_id": submission.id,
            "preview_id": submission.preview_id,
            "bc_id": submission.bc_id,
            "request_id": uuid4(),
            "request_actor_id": context.actor_id,
            "kind": "RETRY",
        }
        values[field] = "other-bc" if field == "bc_id" else uuid4()
        with pytest.raises(IntegrityError):
            session.add(SubmissionRecovery(**values))
            session.flush()
