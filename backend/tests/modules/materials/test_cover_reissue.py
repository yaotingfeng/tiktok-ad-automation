"""显式封面补发保留历史，只接回原批次尚未完成的素材依赖。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.materials import covers
from app.modules.materials.cover_models import (
    MaterialCoverJob,
    MaterialCoverReceipt,
    MaterialCoverShareBatch,
)
from app.modules.materials.cover_reissue import create_cover_replacement
from app.modules.materials.models import AccountMaterial
from tests.modules.builds.test_cover_execution import pending
from tests.modules.builds.test_execution import executable as executable
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def unknown_source(env, redis_client):
    step_id, job_id, submission_id = pending(env, redis_client)
    with Session(env[0]) as db, db.begin():
        job = db.get(MaterialCoverJob, job_id)
        job.purpose, job.status = "SOURCE", "UNKNOWN"
        job.request_armed_at = datetime.now(UTC)
        job.dispatch_id = None
        step = db.get(ExecutionStep, step_id)
        step.status, step.phase = "UNKNOWN", "DONE"
    return step_id, job_id, submission_id


def replace(env, job_id, submission_id):
    from app.modules.materials.reissue_models import MaterialReissueAuthorization

    with Session(env[0]) as db, db.begin():
        result = create_cover_replacement(
            db,
            tenant_id=env[1].tenant_id,
            actor_id=env[1].actor_id,
            submission_id=submission_id,
            cover_job_id=job_id,
        )
        previous = db.exec(
            select(MaterialReissueAuthorization).where(
                MaterialReissueAuthorization.old_cover_job_id == job_id,
            )
        ).first()
        if previous is None:
            db.add(
                MaterialReissueAuthorization(
                    tenant_id=env[1].tenant_id,
                    bc_id=db.get(MaterialCoverJob, job_id).bc_id,
                    submission_id=submission_id,
                    request_id=uuid4(),
                    actor_id=env[1].actor_id,
                    kind="COVER",
                    old_cover_job_id=job_id,
                    new_cover_job_id=UUID(result["new_cover_job_id"]),
                    scope_digest="a" * 64,
                    details=result,
                    accepted_duplicate_materials=True,
                )
            )
        return result


def rejected_build(env, redis_client):
    step_id, job_id, submission_id = pending(env, redis_client)
    with Session(env[0]) as db, db.begin():
        job = db.get(MaterialCoverJob, job_id)
        member = {
            "job_id": str(job.id),
            "source_job_id": str(job.id),
            "material_id": str(job.material_id),
            "advertiser_id": job.advertiser_id,
            "source_mid": "rejected-source-mid",
            "signature": "a" * 32,
            "width": 100,
            "height": 100,
            "share_requested": True,
        }
        batch = MaterialCoverShareBatch(
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            actor_id=job.actor_id,
            source_advertiser_id="source-account",
            source_route=dict(job.frozen_route),
            target_route=dict(job.frozen_route),
            members=[member],
            wake_job_id=job.id,
            status="BLOCKED",
            armed_at=datetime.now(UTC),
            request_digest="a" * 64,
            request_id="rejected-share-request",
            failed_infos={job.advertiser_id: ["rejected-source-mid"]},
        )
        db.add(batch)
        db.flush()
        job.status, job.error_code = "BLOCKED", "cover_share_rejected"
        job.request_armed_at = batch.armed_at
        job.share_batch_id = batch.id
        job.dispatch_id = None
        step = db.get(ExecutionStep, step_id)
        step.status, step.phase, step.error_code = (
            "FAILED",
            "DONE",
            "cover_share_rejected",
        )
        step.dispatch_id = None
    return step_id, job_id, submission_id


def test_explicitly_rejected_build_share_gets_new_generation_without_reusing_attempt(
    executable, redis_client
):
    step_id, old_id, submission_id = rejected_build(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    new_id = UUID(result["new_cover_job_id"])
    with Session(executable[0]) as db:
        old = db.get(MaterialCoverJob, old_id)
        new = db.get(MaterialCoverJob, new_id)
        step = db.get(ExecutionStep, step_id)
        assert old.superseded_by_id == new_id
        assert old.status == "BLOCKED" and old.share_batch_id is not None
        # 平台已明确拒绝这个 source MID 后，新代直接使用目标视频的封面
        # 上传器；继续 BUILD 只会再次选择同一个唯一来源并重复被拒绝。
        assert new.purpose == "SOURCE" and new.status == "PENDING"
        assert new.share_batch_id is None and new.request_armed_at is None
        assert step.cover_job_id == new_id
        assert (step.status, step.phase, step.error_code) == (
            "PENDING",
            "IDLE",
            "cover_pending",
        )


@pytest.mark.parametrize("has_request_id", [False, True])
def test_authorized_unknown_build_share_uses_target_upload_generation(
    executable, redis_client, has_request_id
):
    step_id, old_id, submission_id = rejected_build(executable, redis_client)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        batch = db.get(MaterialCoverShareBatch, old.share_batch_id)
        old.status, old.error_code = "UNKNOWN", "cover_result_unknown"
        batch.status, batch.error_code = "UNKNOWN", "cover_result_unknown"
        batch.failed_infos = {}
        batch.request_id = "unknown-share-request" if has_request_id else None
        step = db.get(ExecutionStep, step_id)
        step.status, step.phase, step.error_code = (
            "UNKNOWN",
            "DONE",
            "cover_result_unknown",
        )

    result = replace(executable, old_id, submission_id)
    new_id = UUID(result["new_cover_job_id"])
    with Session(executable[0]) as db:
        old = db.get(MaterialCoverJob, old_id)
        new = db.get(MaterialCoverJob, new_id)
        step = db.get(ExecutionStep, step_id)
        assert old.superseded_by_id == new_id
        assert old.status == "UNKNOWN" and old.share_batch_id is not None
        assert new.purpose == "SOURCE" and new.status == "PENDING"
        assert new.share_batch_id is None and new.request_armed_at is None
        assert step.cover_job_id == new_id
        assert (step.status, step.phase, step.error_code) == (
            "PENDING",
            "IDLE",
            "cover_pending",
        )


@pytest.mark.parametrize("has_request_id", [False, True])
def test_authorized_claim_lost_build_uses_target_upload_generation(
    executable, redis_client, has_request_id
):
    step_id, old_id, submission_id = rejected_build(executable, redis_client)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        batch = db.get(MaterialCoverShareBatch, old.share_batch_id)
        old.error_code = "cover_claim_lost"
        batch.status, batch.error_code = "BLOCKED", "cover_claim_lost"
        batch.failed_infos = {}
        batch.request_id = "claim-lost-request" if has_request_id else None
        step = db.get(ExecutionStep, step_id)
        step.status, step.phase, step.error_code = (
            "UNKNOWN",
            "DONE",
            "cover_claim_lost",
        )

    result = replace(executable, old_id, submission_id)
    new_id = UUID(result["new_cover_job_id"])
    with Session(executable[0]) as db:
        old = db.get(MaterialCoverJob, old_id)
        new = db.get(MaterialCoverJob, new_id)
        step = db.get(ExecutionStep, step_id)
        assert old.superseded_by_id == new_id
        assert old.status == "BLOCKED" and old.error_code == "cover_claim_lost"
        assert new.purpose == "SOURCE" and new.status == "PENDING"
        assert new.share_batch_id is None and new.request_armed_at is None
        assert step.cover_job_id == new_id
        assert (step.status, step.phase, step.error_code) == (
            "PENDING",
            "IDLE",
            "cover_pending",
        )


def test_reissue_keeps_old_history_and_unknown_ad_and_is_idempotent(
    executable, redis_client
):
    step_id, old_id, submission_id = unknown_source(executable, redis_client)
    with Session(executable[0]) as db, db.begin():
        ad = db.get(ExecutionStep, executable[2]["AD"][0])
        ad.status, ad.phase, ad.request_body = "UNKNOWN", "DONE", {"unknown": True}
        ad_before = ad.model_dump()
        old_before = db.get(MaterialCoverJob, old_id).model_dump()
    result = replace(executable, old_id, submission_id)
    assert result["old_cover_job_id"] == str(old_id)
    new_id = UUID(result["new_cover_job_id"])
    assert new_id != old_id
    assert replace(executable, old_id, submission_id)["new_cover_job_id"] == str(new_id)
    with Session(executable[0]) as db:
        old, new = db.get(MaterialCoverJob, old_id), db.get(MaterialCoverJob, new_id)
        assert old.superseded_by_id == new_id
        assert old.model_dump(exclude={"superseded_by_id"}) == {
            k: v for k, v in old_before.items() if k != "superseded_by_id"
        }
        assert (new.asset_id, new.video_id, new.video_md5, new.frozen_route) == (
            old.asset_id,
            old.video_id,
            old.video_md5,
            old.frozen_route,
        )
        assert new.purpose == "SOURCE" and new.status == "PENDING"
        assert new.request_armed_at is None and new.known_image_id is None
        assert new.remote_name != old.remote_name and new.dispatch_id is not None
        assert db.get(ExecutionStep, executable[2]["AD"][0]).model_dump() == ad_before
        assert len(db.exec(select(MaterialCoverJob)).all()) == 2


def test_unfinished_material_rebinds_and_successful_material_is_untouched(
    executable, redis_client
):
    step_id, old_id, submission_id = unknown_source(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    with Session(executable[0]) as db:
        step = db.get(ExecutionStep, step_id)
        assert step.cover_job_id == UUID(result["new_cover_job_id"])
        assert step.status == "UNKNOWN" and step.request_body is None
        assert result["rebound_material_step_ids"] == [str(step_id)]
        assert db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step_id,
                StepEvidence.conclusion == "MATERIAL_COVER_REISSUED",
            )
        ).one().summary["old_cover_job_id"] == str(old_id)


def test_successful_material_keeps_its_original_cover_reference(
    executable, redis_client
):
    step_id, old_id, submission_id = unknown_source(executable, redis_client)
    with Session(executable[0]) as db, db.begin():
        step = db.get(ExecutionStep, step_id)
        step.status, step.error_code = "SUCCEEDED", None
        before = step.model_dump()
    result = replace(executable, old_id, submission_id)
    assert result["rebound_material_step_ids"] == []
    with Session(executable[0]) as db:
        assert db.get(ExecutionStep, step_id).model_dump() == before


def test_concurrent_reissue_creates_only_one_successor(executable, redis_client):
    _, old_id, submission_id = unknown_source(executable, redis_client)
    barrier = Barrier(2)

    def work():
        barrier.wait(timeout=10)
        return replace(executable, old_id, submission_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0]["new_cover_job_id"] == results[1]["new_cover_job_id"]
    with Session(executable[0]) as db:
        assert len(db.exec(select(MaterialCoverJob)).all()) == 2


def test_new_cover_is_current_and_old_dispatch_cannot_claim(executable, redis_client):
    from app.modules.materials.routes import load_material_route

    _, old_id, submission_id = unknown_source(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        before = old.model_dump()
        prepared = covers.ensure_source_cover(
            db,
            context=executable[1],
            bc_id=old.bc_id,
            material_id=old.material_id,
            advertiser_id=old.advertiser_id,
            task_key="repeated-source-event",
            route=load_material_route(
                old.frozen_route, context=executable[1], bc_id=old.bc_id
            ),
        )
        assert prepared.task_id == UUID(result["new_cover_job_id"])
        assert (
            covers._claim_in_session(
                db, executable[1], old.id, uuid4(), old.revision, read=False
            )
            is None
        )
        assert old.model_dump() == before


def test_new_ready_cover_completes_rebound_original_material(executable, redis_client):
    from app.modules.builds.cover_execution import recover_cover_results

    step_id, old_id, submission_id = unknown_source(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    with Session(executable[0]) as db, db.begin():
        job = db.get(MaterialCoverJob, UUID(result["new_cover_job_id"]))
        job.request_armed_at = datetime.now(UTC)
        covers._publish_result(
            db,
            executable[1],
            job,
            {
                "image_id": "new-image",
                "signature": "a" * 32,
                "material_id": "new-mid",
            },
        )
    assert recover_cover_results(database_engine=executable[0]) == 1
    with Session(executable[0]) as db:
        step = db.get(ExecutionStep, step_id)
        assert step.status == "SUCCEEDED"
        assert step.resolved["mapping"]["image_id"] == "new-image"
        assert db.get(MaterialCoverJob, old_id).status == "UNKNOWN"


def test_new_ad_final_fence_uses_only_current_cover_without_reopening_success(
    executable, redis_client, wire
):
    from app.modules.builds.execution import process_step
    from tests.modules.builds.test_cover_execution import ready_ad_with_cover_job

    ad_id, material_id, old_id, submission_id = ready_ad_with_cover_job(
        executable, redis_client, cover_status="UNKNOWN"
    )
    with Session(executable[0]) as db, db.begin():
        db.get(MaterialCoverJob, old_id).purpose = "SOURCE"
        before = db.get(ExecutionStep, material_id).model_dump()
    result = replace(executable, old_id, submission_id)
    assert result["rebound_material_step_ids"] == []
    with Session(executable[0]) as db, db.begin():
        job = db.get(MaterialCoverJob, UUID(result["new_cover_job_id"]))
        job.request_armed_at = datetime.now(UTC)
        covers._publish_result(
            db,
            executable[1],
            job,
            {
                "image_id": "new-image",
                "signature": "a" * 32,
                "material_id": "900001",
            },
        )
    wire[1].append({"smart_plus_ad_id": "newly-created-ad"})
    assert (
        process_step(
            database_engine=executable[0],
            redis_client=redis_client,
            context=executable[1],
            step_id=ad_id,
            revision=0,
        )
        == "SUCCEEDED"
    )
    assert len(wire[0]) == 1 and wire[0][0][0] == "POST"
    with Session(executable[0]) as db:
        assert db.get(ExecutionStep, material_id).model_dump() == before
        assert db.get(MaterialCoverJob, old_id).status == "UNKNOWN"


def test_late_old_receipt_is_preserved_without_changing_current_mapping(
    executable, redis_client
):
    from app.integrations.tiktok.contracts.materials import ImageReceipt

    _, old_id, submission_id = unknown_source(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        before = old.model_dump()
        db.get(AccountMaterial, old.asset_id).image_id = "new-image"
    covers._preserve_receipt(
        executable[0],
        executable[1],
        old_id,
        ImageReceipt(image_id="late-old", signature="a" * 32),
    )
    with Session(executable[0]) as db:
        old = db.get(MaterialCoverJob, old_id)
        assert old.model_dump() == before
        assert db.get(AccountMaterial, old.asset_id).image_id == "new-image"
        assert (
            db.exec(
                select(MaterialCoverReceipt).where(
                    MaterialCoverReceipt.job_id == old_id,
                )
            )
            .one()
            .image_id
            == "late-old"
        )
        assert (
            db.get(MaterialCoverJob, UUID(result["new_cover_job_id"])).known_image_id
            is None
        )


@pytest.mark.parametrize("fact", ["known", "candidate", "receipt", "claim", "ready"])
def test_positive_evidence_or_active_job_cannot_be_reissued(
    executable, redis_client, fact
):
    _, old_id, submission_id = unknown_source(executable, redis_client)
    with Session(executable[0]) as db, db.begin():
        job = db.get(MaterialCoverJob, old_id)
        if fact == "known":
            job.known_image_id = "known"
        elif fact == "candidate":
            job.candidate_image_id = "candidate"
        elif fact == "receipt":
            db.add(
                MaterialCoverReceipt(
                    tenant_id=job.tenant_id,
                    job_id=old_id,
                    image_id="receipt",
                    receipt_facts={"signature": None},
                )
            )
        elif fact == "claim":
            job.claim_token, job.claimed_until = uuid4(), datetime.now(UTC)
        else:
            job.status = "READY"
    with pytest.raises(DomainError):
        replace(executable, old_id, submission_id)
    with Session(executable[0]) as db:
        assert db.get(MaterialCoverJob, old_id).superseded_by_id is None


def test_superseded_job_cannot_publish_or_invalidate_current_image(
    executable, redis_client
):
    _, old_id, submission_id = unknown_source(executable, redis_client)
    result = replace(executable, old_id, submission_id)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        new = db.get(MaterialCoverJob, UUID(result["new_cover_job_id"]))
        mapping = db.get(AccountMaterial, new.asset_id)
        mapping.image_id = "new-image"
        old_before = old.model_dump()
        covers._publish_result(
            db,
            executable[1],
            old,
            {
                "image_id": "old-image",
                "signature": "a" * 32,
                "material_id": "old-mid",
            },
        )
        covers._invalidate_receipt(db, old)
        assert mapping.image_id == "new-image"
        assert old.model_dump() == old_before


def test_superseded_job_reconciliation_and_retry_never_requeue(
    executable, redis_client
):
    _, old_id, submission_id = unknown_source(executable, redis_client)
    replace(executable, old_id, submission_id)
    with Session(executable[0]) as db, db.begin():
        old = db.get(MaterialCoverJob, old_id)
        before = old.model_dump()
        with pytest.raises(DomainError):
            covers.request_cover_reconciliation(
                db, context=executable[1], job_id=old_id
            )
        with pytest.raises(DomainError):
            covers.request_cover_retry(db, context=executable[1], job_id=old_id)
        assert old.model_dump() == before


def test_authorized_source_upload_and_readback_resume_original_build_cover(
    executable, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.builds.dependency_waits import wake_material_dependencies
    from app.modules.builds.execution import process_step
    from app.modules.materials.models import AccountMaterial
    from app.modules.materials.routes import load_material_route
    from tests.modules.builds.test_drafts import account
    from tests.modules.materials.test_cover_throughput import page

    step_id, target_id, submission_id = pending(executable, redis_client)
    engine, context, _ = executable
    with Session(engine) as db, db.begin():
        target = db.get(MaterialCoverJob, target_id)
        account(db, context, "source-account")
        for grant in db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == context.tenant_id,
            )
        ).all():
            grant.can_upload = True
        source_asset = AccountMaterial(
            tenant_id=context.tenant_id,
            bc_id=target.bc_id,
            material_id=target.material_id,
            advertiser_id="source-account",
            connection_id=target.connection_id,
            video_id="source-video",
            status="available",
            verified_at=datetime.now(UTC),
        )
        db.add(source_asset)
        db.flush()
        source_result = covers.ensure_source_cover(
            db,
            context=context,
            bc_id=target.bc_id,
            material_id=target.material_id,
            advertiser_id="source-account",
            task_key="source-history",
            route=load_material_route(
                target.frozen_route, context=context, bc_id=target.bc_id
            ),
        )
        old = db.get(MaterialCoverJob, source_result.task_id)
        old.status, old.request_armed_at, old.dispatch_id = (
            "UNKNOWN",
            datetime.now(UTC),
            None,
        )
        old_id, video_md5 = old.id, old.video_md5
        target.status, target.error_code, target.dispatch_id = (
            "BLOCKED",
            "cover_source_unavailable",
            None,
        )
        step = db.get(ExecutionStep, step_id)
        step.status, step.phase, step.error_code = (
            "FAILED",
            "DONE",
            "cover_source_unavailable",
        )

    result = replace(executable, old_id, submission_id)
    source_id = UUID(result["new_cover_job_id"])
    assert result["resumed_build_cover_job_ids"] == [str(target_id)]

    def drive(identity, *, read=False):
        with Session(engine) as db:
            job = db.get(MaterialCoverJob, identity)
            dispatch_id, revision = job.dispatch_id, job.revision
        covers.run_cover(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            job_id=identity,
            dispatch_id=dispatch_id,
            revision=revision,
            read=read,
        )
        return dispatch_id, revision

    source_image = {
        "image_id": "source-image",
        "material_id": "900001",
        "signature": "a" * 32,
        "width": 720,
        "height": 1280,
        "displayable": False,
    }
    # 使用真实 SDK/配额/事务，只替换 HTTP transport；上传回执缺 MID 时
    # 必须先只读核验，之后 BUILD 才能取新 SOURCE 共享图片。
    wire[1].extend(
        [
            {
                "list": [
                    {
                        "video_id": "source-video",
                        "signature": video_md5,
                        "width": 720,
                        "height": 1280,
                        "displayable": True,
                        "video_cover_url": "https://example.com/cover.jpg",
                    }
                ]
            },
            {"image_id": "source-image", "signature": "a" * 32},
        ]
    )
    dispatch_id, revision = drive(source_id)
    with Session(engine) as db:
        assert db.get(MaterialCoverJob, source_id).status == "VERIFYING"
    # 同一授权新代的重复投递不能再次发送。
    covers.run_cover(
        database_engine=engine,
        redis_client=redis_client,
        context=context,
        job_id=source_id,
        dispatch_id=dispatch_id,
        revision=revision,
        read=False,
    )
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    wire[1].append({"list": [source_image]})
    drive(source_id, read=True)
    with Session(engine) as db:
        assert db.get(MaterialCoverJob, source_id).status == "READY"
        assert db.get(MaterialCoverJob, old_id).status == "UNKNOWN"
    wire[1].extend([{"list": [source_image]}, page([]), {"failed_infos": {}}])
    drive(target_id)
    with Session(engine) as db:
        job = db.get(MaterialCoverJob, target_id)
        assert job.status == "VERIFYING", job.error_code
    wire[1].append(
        page([{**source_image, "image_id": "target-image", "material_id": "800001"}])
    )
    drive(target_id, read=True)
    with Session(engine) as db:
        job = db.get(MaterialCoverJob, target_id)
        assert job.status == "READY", job.error_code
        assert db.get(AccountMaterial, job.asset_id).image_id == "target-image"
    assert sum(call[0] == "POST" for call in wire[0]) == 2
    assert wake_material_dependencies(database_engine=engine) == 1
    with Session(engine) as db:
        revision = db.get(ExecutionStep, step_id).dispatch_revision
    assert (
        process_step(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            step_id=step_id,
            revision=revision,
        )
        == "SUCCEEDED"
    )
    with Session(engine) as db:
        assert db.get(ExecutionStep, step_id).status == "SUCCEEDED"
