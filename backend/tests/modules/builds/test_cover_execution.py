"""Cover dependencies and recovery preserve the original material/video identity."""

from datetime import UTC, datetime, timedelta
from hashlib import md5

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.builds import recovery
from app.modules.builds.cover_execution import recover_cover_results
from app.modules.builds.execution import process_step
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.material_execution import recover_material_results
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
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
        # 本helper刻意移除既有封面以测试新准备；给该合成已核实视频明确原摘要。
        file = session.get(MaterialFile, step.material_id)
        file.video_md5 = md5(b"synthetic previously verified cover video").hexdigest()
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


def pending(env, _redis_client):
    identity = remove_cover(env)
    # 显式构造旧版本已存在的封面依赖，验证发布后仍可核查原上传身份。
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials.covers import ensure_cover

    with Session(env[0]) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        unit = session.get(BuildUnit, step.unit_id)
        prepared = ensure_cover(
            session,
            context=env[1],
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id=unit.advertiser_id,
            task_key="historical-cover",
            route=load_preview_route(
                session, context=env[1], preview_id=step.preview_id
            ),
        )
        step.status, step.phase, step.error_code = "PENDING", "IDLE", "cover_pending"
        step.cover_job_id = prepared.task_id
        step.dispatch_id = None
        session.add(step)
        return identity, prepared.task_id, step.submission_id


def test_new_single_video_material_prepares_missing_cover(executable, redis_client):
    identity = remove_cover(executable)
    db, context, _ = executable
    assert (
        process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=identity,
            revision=0,
        )
        == "PENDING"
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.error_code == "cover_pending"
        job = session.get(MaterialCoverJob, step.cover_job_id)
        assert job.video_id.startswith("target-") and job.request_armed_at is None
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.prepare_cover"
        )
        assert step.request_body is None


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


def test_reconciled_video_completes_video_stage_then_ad_prepares_only_cover(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
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
        assert step.distribution_id == dist_id and step.cover_job_id is None
        assert step.status == "SUCCEEDED"
        assert not session.exec(select(MaterialCoverJob)).all()
    with Session(db) as session, session.begin():
        for kind in ["MATERIAL", "CTA", "CAMPAIGN", "ADGROUP"]:
            for step_id in ids[kind]:
                step = session.get(ExecutionStep, step_id)
                step.status, step.phase, step.dispatch_id = "SUCCEEDED", "DONE", None
                if kind != "MATERIAL":
                    step.remote_id = f"existing-{kind}"
    assert (
        process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=ids["AD"][0],
            revision=0,
        )
        == "PENDING"
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == "SUCCEEDED" and step.distribution_id == dist_id
        job = session.exec(select(MaterialCoverJob)).one()
        assert job.request_armed_at is None
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "materials.prepare_cover"
        )
        assert session.get(ExecutionStep, ids["AD"][0]).request_body is None


def ready_ad_with_cover_job(env, redis_client, *, cover_status="READY"):
    material_step, job_id, submission_id = pending(env, redis_client)
    db, context, ids = env
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.status = cover_status
        job.request_armed_at = datetime.now(UTC)
        job.known_image_id = "actual-target-cover" if cover_status == "READY" else None
        job.dispatch_id = None
        job.updated_at = datetime.now(UTC) - timedelta(days=1)
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


@pytest.mark.parametrize("cover_status", ["READY", "UNKNOWN", "BLOCKED"])
def test_new_video_ad_requires_verified_cover_without_reopening_completed_steps(
    executable, redis_client, monkeypatch, cover_status
):
    import json

    from urllib3.response import HTTPResponse

    ad_id, material_id, job_id, _ = ready_ad_with_cover_job(
        executable, redis_client, cover_status=cover_status
    )
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
    assert process_step(**args) == (
        "SUCCEEDED" if cover_status == "READY" else "FAILED"
    )
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1
        if cover_status != "READY":
            assert not writes
            assert session.get(ExecutionStep, ad_id).request_body is None
            # 已发送但未知/阻塞的图片沿原任务保留，广告不能通过省略封面绕过。
            assert job.status == cover_status and job.dispatch_id is None
            return
        assert job.status == "READY" and job.known_image_id == "actual-target-cover"
        assert job.dispatch_id is None
        replaced_video = job.video_id
    assert process_step(**args) == "SUCCEEDED"
    assert process_step(**args) == "SUCCEEDED"
    assert len(writes) == 1
    assert len(writes[0]["creative_list"]) == 2
    # fixture 的 MATERIAL 查询无排序，不能假设被替换的必定是 target-0。
    # 按明确的合成视频身份校验完整配对，而非只比较图片集合。
    expected_images = {"target-0": "cover-0", "target-1": "cover-1"}
    expected_images[replaced_video] = "actual-target-cover"
    assert {
        row["creative_info"]["video_info"]["video_id"]: row["creative_info"][
            "image_info"
        ][0]["web_uri"]
        for row in writes[0]["creative_list"]
    } == expected_images
    with Session(db) as session:
        job = session.get(MaterialCoverJob, job_id)
        assert job.status == cover_status and job.dispatch_id is None
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1


@pytest.mark.parametrize("expired", ["cover", "video", "changed_identity"])
def test_saved_unsent_ad_reuses_old_evidence_but_rejects_changed_identity(
    executable, redis_client, monkeypatch, expired
):
    import json
    from uuid import uuid4

    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
    from app.modules.builds.execution import _frozen, prepare_request
    from app.modules.builds.execution_state import arm_request, record_not_sent
    from app.modules.builds.request_compiler import create_arguments, decode_intent
    from app.modules.builds.submissions import claim_step

    ad_id, material_id, job_id, _ = ready_ad_with_cover_job(executable, redis_client)
    db, context, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        job.updated_at = datetime.now(UTC)
        claim = claim_step(session, context=context, step_id=ad_id, owner=uuid4())
        prepared = prepare_request(
            session,
            context=context,
            claim=claim,
            frozen=_frozen(session, context, claim),
        )
        _, body = create_arguments(
            attempt_id=claim.attempt_id,
            intent=decode_intent("AD", prepared),
            channel=claim.route.channel,
        )
        digest = arm_request(session, context=context, claim=claim, body=body)
        record_not_sent(
            session,
            claim=claim,
            error=RemoteCallError(
                "admission_deferred", effect="NOT_SENT", evidence=CallEvidence()
            ),
            retryable=True,
            delay=0,
        )
        stale = datetime.now(UTC) - timedelta(days=1)
        if expired == "cover":
            job.updated_at = stale
        elif expired == "changed_identity":
            job.known_image_id = "replacement-cover"
            session.get(AccountMaterial, job.asset_id).image_id = "replacement-cover"
        else:
            session.get(AccountMaterial, job.asset_id).verified_at = stale

    writes = []

    def wire(_pool, method, url, **kwargs):
        assert expired != "changed_identity"
        assert method == "POST" and "/ad/create/" in url
        writes.append(json.loads(kwargs["body"]))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"smart_plus_ad_id": "actual-ad"}}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    assert process_step(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        step_id=ad_id,
        revision=0,
    ) == ("FAILED" if expired == "changed_identity" else "SUCCEEDED")
    with Session(db) as session:
        step = session.get(ExecutionStep, ad_id)
        assert step.request_body == body and step.request_body_digest == digest
        if expired == "changed_identity":
            assert (
                step.remote_id is None and step.error_code == "execution_intent_changed"
            )
            assert not writes
        else:
            assert step.remote_id == "actual-ad" and step.error_code is None
            assert writes == [body]
        assert session.get(ExecutionStep, material_id).status == "SUCCEEDED"
        job = session.get(MaterialCoverJob, job_id)
        assert job.status == "READY" and job.dispatch_id is None


def test_ad_checks_current_video_evidence_after_admission(
    executable, redis_client, monkeypatch
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
                mapping = session.get(AccountMaterial, job.asset_id)
                mapping.video_id, mapping.image_id = "replaced-after-prepare", None
                session.add(mapping)
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


@pytest.mark.parametrize(
    "changed",
    [
        "none",
        "reused",
        "candidate_unverified",
        "expired",
        "unavailable_video",
        "wrong_image",
        "missing",
        "extra",
    ],
)
def test_frozen_custom_cover_keeps_its_final_fence(executable, redis_client, changed):
    from app.core.errors import DomainError
    from app.modules.builds.cover_execution import validate_ad_assets
    from app.modules.builds.preview_models import BuildUnit, PreviewGroupMaterial

    ad_id, _, job_id, _ = ready_ad_with_cover_job(executable, redis_client)
    db, _, _ = executable
    with Session(db) as session, session.begin():
        job = session.get(MaterialCoverJob, job_id)
        if changed != "expired":
            job.updated_at = datetime.now(UTC)
        if changed in {"reused", "candidate_unverified"}:
            # 已在目标账户核实的复用封面没有图片上传，不伪造上传回执或发送时间。
            job.candidate_image_id = job.known_image_id
            job.known_image_id = job.request_armed_at = None
            job.signature = "a" * 32
            job.width, job.height = 720, 1280
            if changed == "candidate_unverified":
                job.status = "PREPARING"
        session.add(job)
        session.flush()
        step = session.get(ExecutionStep, ad_id)
        unit = session.get(BuildUnit, step.unit_id)
        mappings = session.exec(
            select(AccountMaterial)
            .join(
                PreviewGroupMaterial,
                PreviewGroupMaterial.material_id == AccountMaterial.material_id,
            )
            .where(PreviewGroupMaterial.preview_id == step.preview_id)
            .order_by(PreviewGroupMaterial.position)
        ).all()
        if changed == "unavailable_video":
            mappings[0].status = "result_unknown"
            session.flush()
        body = {
            "creative_list": [
                {
                    "creative_info": {
                        "video_info": {"video_id": mapping.video_id},
                        "image_info": [
                            {
                                "web_uri": "wrong-image"
                                if changed == "wrong_image"
                                else mapping.image_id
                            }
                        ],
                    }
                }
                for mapping in mappings
            ]
        }
        if changed == "missing":
            body["creative_list"][0]["creative_info"].pop("image_info")
        elif changed == "extra":
            body["creative_list"][0]["creative_info"]["image_info"].append(
                {"web_uri": "extra"}
            )
        if changed in {"none", "reused", "expired"}:
            validate_ad_assets(session, step=step, unit=unit, body=body)
        else:
            with pytest.raises(
                DomainError,
                check=lambda error: (
                    error.code
                    == (
                        "invalid_build_request"
                        if changed in {"missing", "extra"}
                        else "material_refresh_required"
                    )
                ),
            ):
                validate_ad_assets(session, step=step, unit=unit, body=body)
