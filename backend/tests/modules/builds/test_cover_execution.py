"""Cover dependencies and recovery preserve the original material/video identity."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
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


@pytest.mark.parametrize("revoked", [False, True, "currency"])
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
        if revoked == "currency":
            account = session.get(
                AdvertiserAccount, (context.tenant_id, job.advertiser_id)
            )
            account.currency = "EUR"
            session.add(account)
        elif revoked:
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


def ready_ad_with_cover_job(env, redis_client, *, cover_status="READY"):
    material_step, job_id, submission_id = pending(env, redis_client)
    db, context, ids = env
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status = cover_status
        job.request_armed_at = datetime.now(UTC)
        job.known_image_id = "actual-target-cover" if cover_status == "READY" else None
        job.dispatch_id = None
        job.updated_at = datetime.now(UTC) - timedelta(
            seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS + 1
        )
        mapping = session.get(AccountMaterial, job.asset_id)
        mapping.image_id = job.known_image_id
        for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP"]:
            for identity in ids[kind]:
                step = session.get(ExecutionStep, identity)
                step.status, step.phase, step.dispatch_id = "SUCCEEDED", "DONE", None
                step.lease_token = step.lease_expires_at = None
                if kind != "MATERIAL":
                    step.remote_id = f"existing-{kind}"
                session.add(step)
        session.add_all([job, mapping])
    return ids["AD"][0], material_step, job_id, submission_id


def test_ad_rechecks_expired_cover_without_reopening_completed_steps(
    executable, redis_client, monkeypatch
):
    import json

    from urllib3.response import HTTPResponse

    ad_id, material_id, job_id, _ = ready_ad_with_cover_job(executable, redis_client)
    db, context, _ = executable
    writes = []

    def wire(_pool, method, url, **kwargs):
        assert method == "POST" and "/ad/create/" in url
        body = json.loads(kwargs["body"])
        writes.append(body)
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"smart_plus_ad_id": "actual-ad"}}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    args = {
        "database_engine": db,
        "redis_client": redis_client,
        "context": context,
        "step_id": ad_id,
        "revision": 0,
    }
    assert process_step(**args) == "PENDING"
    assert not writes
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        assert job.status == "VERIFYING" and job.known_image_id == "actual-target-cover"
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.verify_cover"
        )
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        assert session.get(ExecutionStep, ad_id).request_body is None
        # Simulate the positive dependency receipt; this test covers build-side gating.
        job.status, job.updated_at, job.dispatch_id = "READY", datetime.now(UTC), None
        session.get(ExecutionStep, ad_id).due_at = datetime.now(UTC)
        session.add(job)
    assert process_step(**args) == "SUCCEEDED"
    assert process_step(**args) == "SUCCEEDED"
    assert len(writes) == 1
    assert len(writes[0]["creative_list"]) == 2


def test_ad_prepares_a_separate_cover_for_changed_target_video(
    executable, redis_client, monkeypatch
):
    ad_id, material_id, old_job_id, _ = ready_ad_with_cover_job(
        executable, redis_client
    )
    db, context, _ = executable
    with Session(db) as session, session.begin():
        old_job = session.get(MaterialCoverJob, old_job_id)
        mapping = session.get(AccountMaterial, old_job.asset_id)
        mapping.video_id, mapping.image_id = "new-target-video", None
        mapping.verified_at = datetime.now(UTC)
        session.add(mapping)

    def no_write(*_args, **_kwargs):
        raise AssertionError("AD must wait for its new target cover")

    monkeypatch.setattr("urllib3.PoolManager.request", no_write)
    assert (
        process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=ad_id,
            revision=0,
        )
        == "PENDING"
    )
    with Session(db) as session:
        jobs = session.exec(
            select(MaterialCoverJob).order_by(MaterialCoverJob.video_id)
        ).all()
        assert len(jobs) == 2
        new_job = next(job for job in jobs if job.id != old_job_id)
        assert (
            new_job.video_id == "new-target-video" and new_job.request_armed_at is None
        )
        assert (
            session.get(PendingDispatch, new_job.dispatch_id).task_name
            == "materials.prepare_cover"
        )
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        assert session.get(ExecutionStep, ad_id).request_body is None


def test_retry_of_unarmed_ad_reconciles_original_unknown_cover_without_upload(
    executable, redis_client, monkeypatch
):
    ad_id, material_id, job_id, submission_id = ready_ad_with_cover_job(
        executable, redis_client, cover_status="UNKNOWN"
    )
    db, context, _ = executable

    def no_write(*_args, **_kwargs):
        raise AssertionError("ambiguous cover cannot be uploaded again")

    monkeypatch.setattr("urllib3.PoolManager.request", no_write)
    args = {
        "database_engine": db,
        "redis_client": redis_client,
        "context": context,
        "step_id": ad_id,
        "revision": 0,
    }
    assert process_step(**args) == "FAILED"
    assert process_step(**args) == "FAILED"
    with Session(db) as session:
        step = session.get(ExecutionStep, ad_id)
        assert step.error_code == "cover_result_unknown" and step.request_body is None
        assert session.get(MaterialCoverJob, job_id).dispatch_id is None
    receipt = request(executable, submission_id, kind="RETRY")
    run(executable, receipt)
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        assert job.status == "VERIFYING"
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.verify_cover"
        )
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1


@pytest.mark.parametrize("changed", ["video", "cover_expired"])
def test_ad_checks_current_material_evidence_after_admission(
    executable, redis_client, monkeypatch, changed
):
    from contextlib import contextmanager

    from app.modules.builds import execution

    ad_id, _, job_id, _ = ready_ad_with_cover_job(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        session.get(MaterialCoverJob, job_id).updated_at = datetime.now(UTC)
    actual = execution.admitted_build_call

    @contextmanager
    def change_evidence(*args, **kwargs):
        with actual(*args, **kwargs):
            with Session(db) as session, session.begin():
                job = session.get(MaterialCoverJob, job_id)
                if changed == "video":
                    mapping = session.get(AccountMaterial, job.asset_id)
                    mapping.video_id, mapping.image_id = "replaced-after-prepare", None
                    session.add(mapping)
                else:
                    job.updated_at = datetime.now(UTC) - timedelta(
                        seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS + 1
                    )
                    session.add(job)
            yield

    calls = []

    def no_call(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("stale material reached the official transport")

    monkeypatch.setattr(execution, "admitted_build_call", change_evidence)
    monkeypatch.setattr("urllib3.PoolManager.request", no_call)
    assert (
        process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=ad_id,
            revision=0,
        )
        == "PENDING"
    )
    assert not calls
    with Session(db) as session:
        step = session.get(ExecutionStep, ad_id)
        assert step.error_code == "material_refresh_required"
        assert step.request_body is None and step.remote_id is None
