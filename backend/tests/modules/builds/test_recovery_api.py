"""HTTP recovery boundaries with actual DB transactions and no external calls."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_db
from app.core.errors import DomainError, domain_error_handler
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.builds.recovery_api import router
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery import run, setup_failure
from tests.modules.conftest import create_context


def test_http_receipt_is_original_progress_is_current_and_other_tenant_is_404(
    executable,
):
    db, context, _ = executable
    submission_id, _ = setup_failure(executable)
    with Session(db) as session, session.begin():
        other = create_context(session)
        # This actor can read both tenants; resource scoping must still return404.
        session.add(
            TenantMembership(
                tenant_id=other.tenant_id, user_id=context.actor_id, role="operator"
            )
        )
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DomainError, domain_error_handler)

    def database():
        with Session(db) as session:
            yield session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=context.actor_id
    )
    base = f"/tenants/{context.tenant_id}"
    with TestClient(app) as client:
        request_id = str(uuid4())
        response = client.post(
            f"{base}/submissions/{submission_id}/retry", json={"request_id": request_id}
        )
        assert response.status_code == 202
        original = response.json()
        from app.modules.builds.recovery_models import RecoveryReceipt

        run(executable, RecoveryReceipt.model_validate(original))
        assert (
            client.get(f"{base}/submission-recovery-requests/{request_id}").json()
            == original
        )
        current = client.get(
            f"{base}/submission-recoveries/{original['recovery_id']}"
        ).json()
        assert current["state"] == "COMPLETED" and current["scheduled_count"] == 1
        assert (
            client.post(
                f"{base}/submissions/{submission_id}/retry",
                json={"request_id": request_id},
            ).json()
            == original
        )
        assert (
            client.post(
                f"{base}/submissions/{submission_id}/reconcile",
                json={"request_id": request_id},
            ).status_code
            == 409
        )
        for path in [
            f"submission-recovery-requests/{request_id}",
            f"submission-recoveries/{original['recovery_id']}",
        ]:
            assert client.get(f"/tenants/{other.tenant_id}/{path}").status_code == 404
        assert (
            client.post(
                f"{base}/submissions/{submission_id}/retry",
                json={"request_id": str(uuid4()), "force": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{base}/submissions/{submission_id}/retry",
                json={"request_id": "invalid"},
            ).status_code
            == 422
        )
        with Session(db) as session:
            messages = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "builds.recover_submission"
                )
            ).all()
            assert len(messages) == 1


def test_admin_cannot_execute_recovery_using_revoked_original_actor(executable):
    from app.core.context import TenantContext
    from app.modules.builds import recovery, submissions

    db, context, _ = executable
    submission_id, _ = setup_failure(executable)
    with Session(db) as session, session.begin():
        admin = User(username=f"{uuid4()}", hashed_password="unused", is_superuser=True)
        session.add(admin)
        session.flush()
        admin_context = TenantContext(
            tenant_id=context.tenant_id, actor_id=admin.id, role="platform_admin"
        )
        member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
        member.role = "viewer"
        session.add(member)
    import pytest

    with Session(db) as session:
        view = submissions.get_submission(
            session, context=admin_context, submission_id=submission_id
        )
        assert view.recovery.can_retry is False and view.recovery.reasons == [
            "action_forbidden"
        ]
        with pytest.raises(DomainError, check=lambda e: e.code == "action_forbidden"):
            recovery.request_recovery(
                session,
                context=admin_context,
                submission_id=submission_id,
                request_id=uuid4(),
                kind="RETRY",
            )


def test_recovery_request_actor_does_not_replace_original_execution_actor(executable):
    from app.core.context import TenantContext
    from app.modules.builds import recovery
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.recovery_models import SubmissionRecovery

    db, context, _ = executable
    submission_id, step_id = setup_failure(executable)
    with Session(db) as session, session.begin():
        admin = User(username=f"{uuid4()}", hashed_password="unused", is_superuser=True)
        session.add(admin)
        session.flush()
        admin_context = TenantContext(
            tenant_id=context.tenant_id, actor_id=admin.id, role="platform_admin"
        )
        receipt = recovery.request_recovery(
            session,
            context=admin_context,
            submission_id=submission_id,
            request_id=uuid4(),
            kind="RETRY",
        )
    recovery.process_recovery(
        database_engine=db,
        context=admin_context,
        payload={"recovery_id": str(receipt.recovery_id), "revision": 0},
    )
    with Session(db) as session:
        job = session.get(SubmissionRecovery, receipt.recovery_id)
        step = session.get(ExecutionStep, step_id)
        assert job.request_actor_id == admin_context.actor_id
        assert (
            session.get(PendingDispatch, step.dispatch_id).actor_id == context.actor_id
        )
