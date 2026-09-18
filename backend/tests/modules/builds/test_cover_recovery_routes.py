"""封面回执沿原预览继续，默认连接变化不覆盖冻结授权和绑定代数。"""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.routing import verify_route
from app.modules.builds.cover_execution import recover_cover_results
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.routes import load_preview_route
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.covers import get_cover_status
from app.modules.materials.models import AccountMaterial
from tests.modules.builds.test_cover_execution import pending
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_frozen_routes import replacement_default


@pytest.mark.parametrize(
    "waiting_code", ["cover_pending", "material_pending", "execution_window_wait"]
)
@pytest.mark.parametrize("armed", [False, True])
def test_batch_retry_finds_blocked_cover_before_waiting_step_fails(
    executable, redis_client, waiting_code, armed
):
    from app.core.errors import DomainError
    from app.jobs.models import PendingDispatch
    from app.modules.builds import recovery
    from tests.modules.builds.test_recovery import request, run

    identity, job_id, submission_id = pending(executable, redis_client)
    database, context, _ = executable
    with Session(database) as db, db.begin():
        job = db.get(MaterialCoverJob, job_id)
        job.status, job.error_code, job.dispatch_id = (
            "BLOCKED",
            "cover_search_incomplete",
            None,
        )
        job.request_armed_at = datetime.now(UTC) if armed else None
        step = db.get(ExecutionStep, identity)
        step.error_code = waiting_code
        original_route = job.frozen_route
    if armed:
        with pytest.raises(
            DomainError, check=lambda e: e.code == "recovery_no_candidates"
        ):
            request(executable, submission_id)
        return
    receipt = request(executable, submission_id)
    run(executable, receipt)
    with Session(database) as db:
        progress = recovery.get_recovery(
            db, context=context, recovery_id=receipt.recovery_id
        )
        assert progress.state == "COMPLETED" and progress.scheduled_count == 1
        job = db.get(MaterialCoverJob, job_id)
        assert job.request_armed_at is None and job.frozen_route == original_route
        dispatch = db.get(PendingDispatch, job.dispatch_id)
        assert dispatch.task_name == "materials.prepare_cover"
        assert dispatch.payload == {"job_id": str(job_id), "revision": job.revision}
        first_dispatch = job.dispatch_id
    run(executable, receipt)
    with Session(database) as db:
        assert db.get(MaterialCoverJob, job_id).dispatch_id == first_dispatch


@pytest.mark.parametrize("guard", ["unsent", "armed", "claimed", "dispatched"])
def test_failed_step_resumes_already_pending_cover_without_duplicate_dispatch(
    executable, redis_client, guard
):
    from datetime import timedelta
    from uuid import uuid4

    from app.core.errors import DomainError
    from app.jobs.models import PendingDispatch
    from app.modules.builds import recovery
    from app.modules.builds.execution_window import material_unit_admitted
    from tests.modules.builds.test_recovery import request, run

    identity, job_id, submission_id = pending(executable, redis_client)
    database, context, _ = executable
    with Session(database) as db, db.begin():
        job = db.get(MaterialCoverJob, job_id)
        job.status, job.error_code = "PENDING", "tiktok_call_deadline_exceeded"
        if guard != "dispatched":
            job.dispatch_id = None
        if guard == "armed":
            job.request_armed_at = datetime.now(UTC)
        if guard == "claimed":
            job.claim_token = uuid4()
            job.claimed_until = datetime.now(UTC) + timedelta(minutes=1)
        step = db.get(ExecutionStep, identity)
        step.status, step.phase, step.error_code = (
            "FAILED",
            "DONE",
            "tiktok_call_deadline_exceeded",
        )
        before = len(
            db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "materials.prepare_cover"
                )
            ).all()
        )
    if guard != "unsent":
        with pytest.raises(
            DomainError, check=lambda e: e.code == "recovery_no_candidates"
        ):
            request(executable, submission_id)
        return
    receipt = request(executable, submission_id)
    run(executable, receipt)
    with Session(database) as db:
        progress = recovery.get_recovery(
            db, context=context, recovery_id=receipt.recovery_id
        )
        assert progress.state == "COMPLETED" and progress.scheduled_count == 1
        step, job = db.get(ExecutionStep, identity), db.get(MaterialCoverJob, job_id)
        assert step.status == "QUEUED" and step.dispatch_id is not None
        assert job.status == "PENDING" and job.dispatch_id is None
        assert job.request_armed_at is None
        assert (
            len(
                db.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name == "materials.prepare_cover"
                    )
                ).all()
            )
            == before
        )
        assert material_unit_admitted(
            db,
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            unit_id=step.unit_id,
        )


@pytest.mark.parametrize("change", ["default", "binding", "build_permission"])
def test_cover_recovery_preserves_original_route(executable, redis_client, change):
    identity, job_id, _ = pending(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.request_armed_at = datetime.now(UTC)
        job.status, job.known_image_id, job.dispatch_id = (
            "READY",
            "actual-target-cover",
            None,
        )
        asset = session.get(AccountMaterial, job.asset_id)
        asset.image_id = job.known_image_id
        step = session.get(ExecutionStep, identity)
        step.status, step.phase = "UNKNOWN", "DONE"
        session.add_all([job, asset, step])
        route = load_preview_route(session, context=context, preview_id=step.preview_id)
        # 原连接始终保留；切换默认项后仍只有原连接持有该广告账户授权。
        replacement_default(session, context, route)
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=job.advertiser_id,
            capability="build",
        )
        assert (
            get_cover_status(session, context=context, job_id=job_id).state == "ready"
        )
        if change == "binding":
            binding = session.get(
                BCConnectionBinding,
                (context.tenant_id, route.bc_id, route.connection_id),
            )
            binding.revision += 1
            session.add(binding)
        elif change == "build_permission":
            authorization = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == route.connection_id
                )
            ).one()
            authorization.permission_summary = {
                **authorization.permission_summary,
                "build_authorized": False,
            }
            session.add(authorization)
    recovered = change == "default"
    assert recover_cover_results(database_engine=db) == int(recovered)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == ("SUCCEEDED" if recovered else "UNKNOWN")
        assert step.error_code is None if recovered else step.error_code is not None
        assert step.cover_job_id == job_id
        if recovered:
            assert step.resolved["mapping"]["connection_id"] == str(route.connection_id)
            assert step.resolved["mapping"]["image_id"] == "actual-target-cover"


@pytest.mark.parametrize("armed", [False, True])
def test_delayed_ready_cover_recovery_reuses_successful_receipt_without_age_reads(
    executable, redis_client, armed
):
    from datetime import timedelta

    identity, job_id, _ = pending(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status, job.dispatch_id = "READY", None
        job.known_image_id = "actual-cover" if armed else None
        job.candidate_image_id = None if armed else "actual-cover"
        job.request_armed_at = datetime.now(UTC) if armed else None
        job.updated_at = datetime.now(UTC) - timedelta(days=1)
        asset = session.get(AccountMaterial, job.asset_id)
        asset.image_id = "actual-cover"
        step = session.get(ExecutionStep, identity)
        step.status, step.phase = "UNKNOWN", "DONE"
        session.add_all([job, asset, step])
    assert recover_cover_results(database_engine=db) == 1
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        assert job.status == "READY" and job.dispatch_id is None
        assert session.get(ExecutionStep, identity).status == "SUCCEEDED"
    assert recover_cover_results(database_engine=db) == 0
