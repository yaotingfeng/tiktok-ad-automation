"""Cover dependencies and recovery preserve the original material/video identity."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import recovery
from app.modules.builds.cover_execution import recover_cover_results
from app.modules.builds.execution import process_step
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.material_execution import recover_material_results
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_material_execution import unresolved
from tests.modules.builds.test_recovery import request, run


def remove_cover(env):
    db, _, ids = env
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        asset = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == step.material_id,
            )
        ).one()
        asset.image_id = None
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == step.tenant_id,
                BCAccountAccess.advertiser_id == asset.advertiser_id,
            )
        ).one()
        grant.can_upload = True
        session.add(grant)
        session.add(asset)
    return ids["MATERIAL"][0]


def pending(env, redis_client):
    identity = remove_cover(env)
    outcome = process_step(
        database_engine=env[0],
        redis_client=redis_client,
        context=env[1],
        step_id=identity,
        revision=0,
    )
    with Session(env[0]) as session:
        step = session.get(ExecutionStep, identity)
        assert outcome == "PENDING", step.error_code
        assert step.error_code == "cover_pending"
        assert step.cover_job_id is not None and step.distribution_id is None
        job = session.get(MaterialCoverJob, step.cover_job_id)
        assert job.video_id.startswith("target-") and job.request_armed_at is None
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.prepare_cover"
        )
        return identity, job.id, step.submission_id


@pytest.mark.parametrize("revoked", [False, True])
def test_verified_cover_wakes_original_step_only_with_current_actor(
    executable, redis_client, revoked
):
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
        if revoked:
            member = session.exec(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == context.tenant_id,
                    TenantMembership.user_id == context.actor_id,
                )
            ).one()
            member.role = "viewer"
        session.add_all([job, asset, step])
    assert recover_cover_results(database_engine=db) == (0 if revoked else 1)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == ("UNKNOWN" if revoked else "SUCCEEDED")
        if not revoked:
            assert step.resolved["mapping"]["image_id"] == "actual-target-cover"
            assert step.resolved["mapping"]["video_id"].startswith("target-")


@pytest.mark.parametrize("armed", [False, True])
def test_cover_retry_or_reconcile_keeps_job_identity(executable, redis_client, armed):
    identity, job_id, submission_id = pending(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status, job.dispatch_id = ("UNKNOWN" if armed else "BLOCKED"), None
        job.error_code = "cover_result_unknown" if armed else "admission_unavailable"
        if armed:
            job.request_armed_at = datetime.now(UTC)
        step = session.get(ExecutionStep, identity)
        step.status, step.phase, step.dispatch_id = (
            ("UNKNOWN" if armed else "FAILED"),
            "DONE",
            None,
        )
        session.add_all([step, job])
    with Session(db) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        summary = recovery.recovery_summary(
            session, context=context, submission=session.get(Submission, submission_id)
        )
        assert summary.can_retry is (not armed)
        assert summary.can_reconcile is armed
    receipt = request(executable, submission_id, kind="RECONCILE" if armed else "RETRY")
    run(executable, receipt)
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        assert dispatch.task_name == (
            "materials.verify_cover" if armed else "materials.prepare_cover"
        )
        assert dispatch.payload["job_id"] == str(job_id)
        assert session.get(ExecutionStep, identity).cover_job_id == job_id
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1


def test_reconciled_video_without_cover_prepares_only_separate_cover(
    executable, monkeypatch
):
    db, context, _ = executable
    identity = remove_cover(executable)
    dist_id = unresolved(db, context, identity, status="ready")

    def no_video(*_args, **_kwargs):
        raise AssertionError("positively verified video must never upload again")

    monkeypatch.setattr(
        "app.modules.materials.distribution.ensure_target_asset", no_video
    )
    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_video)
    recovered = recover_material_results(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert recovered == 1, step.error_code
        assert step.distribution_id == dist_id and step.cover_job_id is not None
        assert step.status == "PENDING"
        job = session.get(MaterialCoverJob, step.cover_job_id)
        assert job.request_armed_at is None
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.prepare_cover"
        )
