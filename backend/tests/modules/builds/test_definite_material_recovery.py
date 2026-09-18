"""原批次只重试有明确未生效证据的素材，PG 锁和官方 SDK 传输边界保持真实。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.builds.execution_models import ExecutionStep, StepEvidence
from app.modules.builds.recovery_models import SubmissionRecovery
from app.modules.builds.routes import load_preview_route
from app.modules.materials.distribution import ensure_target_asset, run_distribution
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from tests.modules.builds.test_drafts import account
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery import request, run


@pytest.fixture
def rejected_material(executable, redis_client, monkeypatch, request):
    from app.modules.accounts.models import BCAccountAccess

    database, context, ids = executable
    mode = getattr(request, "param", "single")
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": {**settings.TIKTOK_CALL_POLICIES["base"], "lease_ms": 970000}},
    )
    source_rows = []
    prepared_ids = []
    with Session(database) as db, db.begin():
        account(db, context, "source-account")
        for index, identity in enumerate(
            ids["MATERIAL"][: 2 if mode == "batch" else 1]
        ):
            step = db.get(ExecutionStep, identity)
            material = db.get(MaterialFile, step.material_id)
            material.video_md5 = "a" * 32
            material.storage_state = "unavailable"
            old_mapping = db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == material.id
                )
            ).one()
            db.delete(old_mapping)
            route = load_preview_route(db, context=context, preview_id=step.preview_id)
            source_video = "source-video" if index == 0 else "source-video-2"
            source_mid = "source-mid" if index == 0 else "source-mid-2"
            db.add(
                AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id=step.bc_id,
                    material_id=material.id,
                    advertiser_id="source-account",
                    connection_id=route.connection_id,
                    video_id=source_video,
                    mid=source_mid,
                    status="available",
                    verified_at=datetime.now(UTC),
                )
            )
            db.flush()
            prepared = ensure_target_asset(
                db,
                context=context,
                bc_id=step.bc_id,
                material_id=material.id,
                advertiser_id="account-A",
                task_key=f"build:{step.id}",
                route=route,
            )
            assert prepared.state == "queued"
            step.distribution_id = prepared.task_id
            step.status, step.phase, step.error_code = (
                "FAILED",
                "DONE",
                "material_share_failed",
            )
            step.dispatch_id = None
            prepared_ids.append((step.id, step.submission_id, prepared.task_id))
            source_rows.append(
                {
                    "video_id": source_video,
                    "material_id": source_mid,
                    "file_name": material.file_name,
                    "signature": "a" * 32,
                    "displayable": True,
                }
            )
        if mode == "unsent":
            db.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.advertiser_id == "account-A"
                )
            ).one().can_build = False
        # 模拟 outbox 已完成发布；真实处理结果由下方 SDK 回执产生。
        for dispatch in db.exec(select(PendingDispatch)).all():
            dispatch.published_at = datetime.now(UTC)
        identity, submission_id, distribution_id = prepared_ids[0]

    calls = []

    def transport(_pool, _method, url, **_kwargs):
        calls.append(url)
        data = (
            {"failed_infos": {"account-A": ["source-mid"]}}
            if "/share/" in url
            else {"list": source_rows}
        )
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "request_id": "rejected-share", "data": data}
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    run_distribution(
        database_engine=database,
        redis_client=redis_client,
        context=context,
        distribution_id=distribution_id,
        kind="prepare",
    )
    with Session(database) as db, db.begin():
        if mode == "unsent":
            assert calls == []
            db.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.advertiser_id == "account-A"
                )
            ).one().can_build = True
        for dispatch in db.exec(select(PendingDispatch)).all():
            dispatch.published_at = datetime.now(UTC)
        dist = db.get(MaterialDistribution, distribution_id)
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        assert (dist.status, operation.status) == ("blocked", "failed")
        assert operation.remote_response["definite_no_effect"] is True
        before = (dist.model_dump(), operation.model_dump())
    return identity, submission_id, distribution_id, before, calls


@pytest.mark.parametrize(
    "rejected_material", ["single", "batch", "unsent"], indirect=True
)
def test_explicit_retry_rebinds_rejected_share_without_rewriting_original_evidence(
    executable,
    rejected_material,
):
    database, context, _ = executable
    identity, submission_id, distribution_id, before, calls = rejected_material
    call_count = len(calls)
    receipt = request(executable, submission_id)
    assert request(executable, submission_id, request_id=receipt.request_id) == receipt
    run(executable, receipt)
    run(executable, receipt)
    with Session(database) as db:
        job = db.get(SubmissionRecovery, receipt.recovery_id)
        assert (job.state, job.scheduled_count) == ("COMPLETED", 1)
        step = db.get(ExecutionStep, identity)
        assert step.status == "QUEUED" and step.distribution_id != distribution_id
        old_dist = db.get(MaterialDistribution, distribution_id)
        old_operation = db.get(MaterialAssetOperation, old_dist.operation_id)
        assert (old_dist.model_dump(), old_operation.model_dump()) == before
        new_dist = db.get(MaterialDistribution, step.distribution_id)
        assert (
            new_dist.path == "share_source"
            and new_dist.operation_id != old_operation.id
        )
        assert new_dist.target_route == old_dist.target_route
        assert new_dist.source_route == old_dist.source_route
        assert new_dist.source_asset_id == old_dist.source_asset_id
        audit = db.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == identity,
                StepEvidence.conclusion == "RETRY_REQUESTED",
            )
        ).one()
        assert audit.summary["recovery_id"] == str(receipt.recovery_id)
        assert audit.summary["old_distribution_id"] == str(distribution_id)
        assert audit.summary["new_distribution_id"] == str(new_dist.id)
        assert (
            db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == step.material_id,
                    AccountMaterial.advertiser_id == "account-A",
                )
            ).first()
            is None
        )
    assert len(calls) == call_count


@pytest.mark.parametrize(
    "guard",
    [
        "unknown",
        "no_proof",
        "claimed",
        "dispatch",
        "video",
        "upload_video_id",
        "verified_upload_video_id",
        "conflicting_video_id",
        "candidates",
        "read_only_operation",
        "step_mapping",
        "active_distribution",
        "mapping",
        "acknowledged",
        "wrong_target",
        "armed_without_receipt",
        "source_owned",
        "changed_content",
        "changed_binding",
        "read_only",
    ],
)
def test_retry_rejects_unsafe_or_changed_material_evidence(
    executable, rejected_material, guard
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.materials.models import MaterialUploadAttempt

    database, context, _ = executable
    identity, submission_id, distribution_id, _, calls = rejected_material
    call_count = len(calls)
    with Session(database) as db, db.begin():
        step = db.get(ExecutionStep, identity)
        dist = db.get(MaterialDistribution, distribution_id)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        response = dict(op.remote_response)
        if guard == "unknown":
            op.status, dist.status, step.status = (
                "result_unknown",
                "result_unknown",
                "UNKNOWN",
            )
        elif guard == "no_proof":
            response.pop("definite_no_effect")
        elif guard == "claimed":
            op.attempt_token, op.claimed_until = (
                uuid4(),
                datetime.now(UTC) + timedelta(minutes=1),
            )
        elif guard == "dispatch":
            db.add(
                PendingDispatch(
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    task_name="materials.prepare_target",
                    task_key=f"synthetic:{uuid4()}",
                    payload={
                        "distribution_id": str(dist.id),
                        "operation_id": str(op.id),
                    },
                )
            )
        elif guard == "video":
            response["video_id"] = "actual-target-video"
        elif guard in {
            "upload_video_id",
            "verified_upload_video_id",
            "conflicting_video_id",
        }:
            response[guard] = "actual-target-video"
        elif guard == "candidates":
            response["candidates"] = [{"video_id": "possible-target-video"}]
        elif guard == "read_only_operation":
            response["read_only"] = True
        elif guard == "step_mapping":
            step.resolved = {"mapping": {"video_id": "actual-target-video"}}
        elif guard == "active_distribution":
            db.add(
                MaterialDistribution(
                    **{**dist.model_dump(), "id": uuid4(), "status": "queued"}
                )
            )
        elif guard == "mapping":
            db.add(
                AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id=dist.bc_id,
                    material_id=dist.material_id,
                    advertiser_id=dist.advertiser_id,
                    connection_id=db.get(
                        AccountMaterial, dist.source_asset_id
                    ).connection_id,
                    video_id="actual-target-video",
                    status="unavailable",
                )
            )
        elif guard == "acknowledged":
            response["share_acknowledged"] = True
        elif guard == "wrong_target":
            response["share_receipt"] = {
                "failed_infos": {"other-target": ["source-mid"]}
            }
        elif guard == "armed_without_receipt":
            response.pop("share_receipt")
        elif guard == "source_owned":
            db.add(
                MaterialUploadAttempt(
                    tenant_id=context.tenant_id,
                    bc_id=dist.bc_id,
                    material_id=dist.material_id,
                    advertiser_id=dist.advertiser_id,
                    connection_id=db.get(
                        AccountMaterial, dist.source_asset_id
                    ).connection_id,
                    operation_id=op.id,
                    request_digest=op.request_digest,
                    status="failed",
                )
            )
        elif guard == "changed_content":
            db.get(MaterialFile, dist.material_id).video_md5 = "b" * 32
        elif guard == "changed_binding":
            binding = db.get(
                BCConnectionBinding,
                (
                    context.tenant_id,
                    dist.bc_id,
                    db.get(AccountMaterial, dist.source_asset_id).connection_id,
                ),
            )
            binding.revision += 1
        op.remote_response = response
    with pytest.raises(DomainError):
        request(
            executable,
            submission_id,
            kind="RECONCILE" if guard == "read_only" else "RETRY",
        )
    with Session(database) as db:
        assert db.get(ExecutionStep, identity).distribution_id == distribution_id
        assert len(db.exec(select(MaterialDistribution)).all()) == (
            2 if guard == "active_distribution" else 1
        )
    assert len(calls) == call_count


def test_changed_source_video_cannot_be_substituted_during_retry(
    executable, rejected_material
):
    database, _, _ = executable
    identity, submission_id, distribution_id, _, _ = rejected_material
    receipt = request(executable, submission_id)
    with Session(database) as db, db.begin():
        old = db.get(MaterialDistribution, distribution_id)
        db.get(AccountMaterial, old.source_asset_id).video_id = "different-video"
    run(executable, receipt)
    with Session(database) as db:
        job = db.get(SubmissionRecovery, receipt.recovery_id)
        assert job.state == "FAILED" and job.scheduled_count == 0
        assert db.get(ExecutionStep, identity).distribution_id == distribution_id
        assert len(db.exec(select(MaterialDistribution)).all()) == 1


def test_competing_recovery_workers_rebind_original_step_once(
    executable, rejected_material
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    database, _, _ = executable
    identity, submission_id, distribution_id, _, _ = rejected_material
    receipts = [request(executable, submission_id), request(executable, submission_id)]
    barrier = Barrier(2)

    def recover(receipt):
        barrier.wait(timeout=10)
        run(executable, receipt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover, receipt) for receipt in receipts]
        for future in futures:
            future.result(timeout=15)
    with Session(database) as db:
        assert (
            sum(
                db.get(SubmissionRecovery, r.recovery_id).scheduled_count
                for r in receipts
            )
            == 1
        )
        step = db.get(ExecutionStep, identity)
        assert step.distribution_id != distribution_id and step.dispatch_revision == 1
        assert len(db.exec(select(MaterialDistribution)).all()) == 2


def test_native_share_cannot_fall_back_to_original_upload(
    executable, rejected_material, monkeypatch
):
    database, _, _ = executable
    identity, submission_id, distribution_id, _, calls = rejected_material
    receipt = request(executable, submission_id)
    # 真实原文件配置使 upload_original 路径确实可用；恢复仍必须拒绝替换原共享。
    for key, value in {
        "S3_BUCKET": "synthetic",
        "S3_ACCESS_KEY_ID": "synthetic",
        "S3_SECRET_ACCESS_KEY": "synthetic",
    }.items():
        monkeypatch.setattr(settings, key, value)
    with Session(database) as db, db.begin():
        old = db.get(MaterialDistribution, distribution_id)
        db.get(AccountMaterial, old.source_asset_id).status = "unavailable"
        db.get(MaterialFile, old.material_id).storage_state = "stored"
    before_calls = len(calls)
    run(executable, receipt)
    with Session(database) as db:
        assert db.get(SubmissionRecovery, receipt.recovery_id).state == "FAILED"
        assert db.get(ExecutionStep, identity).distribution_id == distribution_id
        assert len(db.exec(select(MaterialDistribution)).all()) == 1
        assert all(
            op.path == "share_source"
            for op in db.exec(select(MaterialAssetOperation)).all()
        )
    assert len(calls) == before_calls


def test_rebound_distribution_runs_native_share_and_publishes_only_verified_target(
    executable, rejected_material, redis_client, monkeypatch
):
    database, context, _ = executable
    identity, submission_id, old_id, old_evidence, _ = rejected_material
    receipt = request(executable, submission_id)
    run(executable, receipt)
    with Session(database) as db:
        new_id = db.get(ExecutionStep, identity).distribution_id
        material_id = db.get(MaterialDistribution, new_id).material_id
        filename = db.get(MaterialFile, material_id).file_name
    calls = []

    def transport(_pool, method, url, **_kwargs):
        calls.append((method, url))
        assert "/upload/" not in url
        if "/share/" in url:
            data = {}
        else:
            data = {
                "list": [
                    {
                        "video_id": "source-video"
                        if "/info/" in url
                        else "verified-target",
                        "material_id": "source-mid",
                        "file_name": filename,
                        "signature": "a" * 32,
                        "displayable": True,
                    }
                ],
                "page_info": {"page": 1, "page_size": 100, "total_page": 1},
            }
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": data}).encode(), status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    for kind in ("prepare", "verify"):
        run_distribution(
            database_engine=database,
            redis_client=redis_client,
            context=context,
            distribution_id=new_id,
            kind=kind,
        )
        if kind == "prepare":
            with Session(database) as db:
                assert db.get(MaterialDistribution, new_id).status == "verifying"
                assert (
                    db.exec(
                        select(AccountMaterial).where(
                            AccountMaterial.material_id == material_id,
                            AccountMaterial.advertiser_id == "account-A",
                        )
                    ).first()
                    is None
                )
    with Session(database) as db:
        dist = db.get(MaterialDistribution, new_id)
        assert dist.status == "ready"
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == material_id,
                AccountMaterial.advertiser_id == "account-A",
            )
        ).one()
        assert mapping.video_id == "verified-target"
        old = db.get(MaterialDistribution, old_id)
        assert (
            old.model_dump(),
            db.get(MaterialAssetOperation, old.operation_id).model_dump(),
        ) == old_evidence
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("rejected_material", ["batch"], indirect=True)
@pytest.mark.parametrize("change", ["wrong_pair", "unknown_receipt", "active_member"])
def test_batch_negative_proof_cannot_override_contradictory_receipt(
    executable, rejected_material, change
):
    from app.modules.materials.batch_models import (
        MaterialShareBatchMember,
        MaterialShareBatchReceipt,
    )

    database, _, _ = executable
    identity, submission_id, distribution_id, _, _ = rejected_material
    with Session(database) as db, db.begin():
        member = db.exec(
            select(MaterialShareBatchMember).where(
                MaterialShareBatchMember.distribution_id == distribution_id
            )
        ).one()
        receipt = db.exec(
            select(MaterialShareBatchReceipt).where(
                MaterialShareBatchReceipt.batch_id == member.batch_id
            )
        ).one()
        if change == "wrong_pair":
            db.add(
                MaterialShareBatchReceipt(
                    tenant_id=receipt.tenant_id,
                    bc_id=receipt.bc_id,
                    batch_id=receipt.batch_id,
                    effect="ACKNOWLEDGED",
                    share_response={"failed_infos": {"other-target": ["source-mid"]}},
                )
            )
        elif change == "unknown_receipt":
            db.add(
                MaterialShareBatchReceipt(
                    tenant_id=receipt.tenant_id,
                    bc_id=receipt.bc_id,
                    batch_id=receipt.batch_id,
                    effect="UNKNOWN",
                )
            )
        else:
            member.status = "pending"
    with pytest.raises(DomainError):
        request(executable, submission_id)
    with Session(database) as db:
        assert db.get(ExecutionStep, identity).distribution_id == distribution_id
        assert len(db.exec(select(MaterialDistribution)).all()) == 2
