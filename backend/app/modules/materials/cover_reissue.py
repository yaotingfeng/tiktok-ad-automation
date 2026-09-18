"""明确授权的 SOURCE 封面新代；授权账本由调用方在同一事务提交。"""

from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import evidence
from app.modules.builds.preview_models import BuildUnit, PreviewGroupMaterial
from app.modules.tenants.permissions import require_tenant

from . import covers
from .cover_models import (
    MaterialCoverJob,
    MaterialCoverReceipt,
    MaterialCoverShareBatch,
)
from .models import MaterialFile


def create_cover_replacement(
    session: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    submission_id: UUID,
    cover_job_id: UUID,
) -> dict[str, Any]:
    """只登记新发送身份；绝不抹掉原未知发送，也不直接调用平台。"""
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    require_tenant(session, actor_id=actor_id, tenant_id=tenant_id, action="upload")
    submission = session.exec(
        select(Submission).where(
            Submission.tenant_id == tenant_id,
            Submission.id == submission_id,
        )
    ).one_or_none()
    original = covers._job(session, context, cover_job_id)
    if submission is None or original.bc_id != submission.bc_id:
        raise DomainError("material_reissue_scope_mismatch", "封面不属于指定投放批次")
    member = session.exec(
        select(PreviewGroupMaterial.material_id)
        .where(
            PreviewGroupMaterial.tenant_id == tenant_id,
            PreviewGroupMaterial.preview_id == submission.preview_id,
            PreviewGroupMaterial.material_id == original.material_id,
        )
        .limit(1)
    ).first()
    if member is None:
        raise DomainError("material_reissue_scope_mismatch", "封面素材不在原批次中")

    # 与正式素材规划保持 unit→step→material→cover 锁序。提前锁本批次
    # 同素材的封面消费者，以便原 SOURCE 和其未发送 BUILD 依赖一起接续。
    consumer_query = select(ExecutionStep).where(
        ExecutionStep.tenant_id == tenant_id,
        ExecutionStep.submission_id == submission_id,
        ExecutionStep.kind == "MATERIAL",
        ExecutionStep.material_id == original.material_id,
        col(ExecutionStep.cover_job_id).is_not(None),
    )
    unit_ids = select(ExecutionStep.unit_id).where(
        ExecutionStep.tenant_id == tenant_id,
        ExecutionStep.submission_id == submission_id,
        ExecutionStep.kind == "MATERIAL",
        ExecutionStep.material_id == original.material_id,
        col(ExecutionStep.cover_job_id).is_not(None),
    )
    session.exec(
        select(SubmissionUnit)
        .where(
            SubmissionUnit.tenant_id == tenant_id,
            SubmissionUnit.submission_id == submission_id,
            col(SubmissionUnit.unit_id).in_(unit_ids),
        )
        .order_by(col(SubmissionUnit.unit_id))
        .with_for_update()
    ).all()
    consumers = session.exec(
        consumer_query.order_by(col(ExecutionStep.id)).with_for_update()
    ).all()
    session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == tenant_id,
            MaterialFile.id == original.material_id,
        )
        .with_for_update()
    ).one()
    old = covers._job(session, context, cover_job_id, lock=True)
    if old.superseded_by_id is not None:
        # 重复确认只能返回原替代身份；不能沿链再自动补发下一代。
        new = covers._job(session, context, old.superseded_by_id)
        return {
            "old_cover_job_id": str(old.id),
            "new_cover_job_id": str(new.id),
            "rebound_material_step_ids": [],
            "resumed_build_cover_job_ids": [],
        }
    has_receipt = (
        session.exec(
            select(MaterialCoverReceipt.id)
            .where(
                MaterialCoverReceipt.tenant_id == tenant_id,
                MaterialCoverReceipt.job_id == old.id,
            )
            .limit(1)
        ).first()
        is not None
    )
    rejected_batch = (
        session.get(MaterialCoverShareBatch, old.share_batch_id)
        if old.share_batch_id is not None
        else None
    )
    rejected_member = (
        next(
            (
                member
                for member in rejected_batch.members
                if member.get("job_id") == str(old.id)
            ),
            None,
        )
        if rejected_batch is not None
        else None
    )
    explicitly_rejected_build = bool(
        old.purpose == "BUILD"
        and old.status == "BLOCKED"
        and old.error_code == "cover_share_rejected"
        and rejected_batch is not None
        and rejected_batch.request_id
        and rejected_member
        and rejected_member.get("share_requested") is True
        and rejected_member.get("source_mid")
        in rejected_batch.failed_infos.get(old.advertiser_id, [])
    )
    unknown_source = bool(
        old.purpose == "SOURCE"
        and old.status == "UNKNOWN"
        and old.share_batch_id is None
        and old.image_mid is None
    )
    if (
        not (unknown_source or explicitly_rejected_build)
        or old.request_armed_at is None
        or old.known_image_id is not None
        or old.candidate_image_id is not None
        or has_receipt
        or old.claim_token is not None
        or old.claimed_until is not None
    ):
        raise DomainError(
            "cover_reissue_forbidden", "只有无已知图片回执的历史未知源封面可补发"
        )
    covers._access(session, context, old, upload=True)
    execution_context = TenantContext(
        tenant_id=tenant_id, actor_id=old.actor_id, role="operator"
    )
    covers._access(session, execution_context, old, upload=True)
    mapping = covers._mapping(session, old)
    if mapping is None or mapping.image_id is not None:
        raise DomainError("cover_reissue_forbidden", "已有封面正证据，应核验原图片")

    identity = uuid4()
    # deferred self-FK 允许先退下旧 current 再插入新代，唯一索引始终只
    # 容纳一个 current；事务失败会同时回滚指针、新代和授权账本。
    old.superseded_by_id = identity
    session.flush()
    new = MaterialCoverJob(
        id=identity,
        tenant_id=old.tenant_id,
        bc_id=old.bc_id,
        material_id=old.material_id,
        asset_id=old.asset_id,
        advertiser_id=old.advertiser_id,
        connection_id=old.connection_id,
        actor_id=old.actor_id,
        frozen_route=dict(old.frozen_route or {}),
        video_id=old.video_id,
        video_md5=old.video_md5,
        remote_name=f"cover-{identity.hex}.jpg",
        # 明确共享拒绝说明目标没有收到图片；新代直接复用 SOURCE 上传器，
        # 从目标视频取封面并使用上传回执 ID，避免再次选择同一坏源 MID。
        purpose="SOURCE" if explicitly_rejected_build else old.purpose,
    )
    session.add(new)
    session.flush()
    covers._queue(session, new, read=False)

    rebound = []
    for step in consumers:
        # MATERIAL 的平台视频正证据和成功事实不重开；AD 无论状态如何
        # 都不在修改集合中，尤其不能把原 UNKNOWN AD 当成未发送广告。
        if (
            step.cover_job_id != old.id
            or step.status in {"SUCCEEDED", "RUNNING"}
            or step.remote_id
            or step.request_body is not None
            or step.lease_token is not None
            or step.dispatch_id is not None
        ):
            continue
        unit = session.get(BuildUnit, step.unit_id)
        from app.modules.builds.cover_execution import cover_matches_step

        if unit is None or not cover_matches_step(new, step, unit):
            continue
        step.cover_job_id = new.id
        step.status, step.phase, step.error_code = (
            ("PENDING", "IDLE", "cover_pending")
            if old.purpose == "BUILD"
            else ("UNKNOWN", "DONE", "cover_pending")
        )
        step.dispatch_id = step.lease_token = step.lease_expires_at = None
        step.updated_at = covers._now()
        evidence(
            session,
            step=step,
            claim=None,
            conclusion="MATERIAL_COVER_REISSUED",
            summary={"old_cover_job_id": str(old.id), "new_cover_job_id": str(new.id)},
        )
        rebound.append(str(step.id))

    # 被旧 SOURCE 失败阻塞、但从未发送的原批次 BUILD 封面接回正常
    # 来源等待链；已有 IMAGE 分享批次保留冻结成员，不能改写 source_job_id。
    target_accounts = select(BuildUnit.advertiser_id).where(
        BuildUnit.tenant_id == tenant_id,
        BuildUnit.preview_id == submission.preview_id,
    )
    dependent_jobs = select(ExecutionStep.cover_job_id).where(
        ExecutionStep.tenant_id == tenant_id,
        ExecutionStep.submission_id == submission_id,
        ExecutionStep.kind == "MATERIAL",
        col(ExecutionStep.cover_job_id).is_not(None),
        col(ExecutionStep.status).not_in(["SUCCEEDED", "RUNNING"]),
        col(ExecutionStep.request_body).is_(None),
        col(ExecutionStep.remote_id).is_(None),
        col(ExecutionStep.lease_token).is_(None),
        col(ExecutionStep.dispatch_id).is_(None),
    )
    waiting = session.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == tenant_id,
            MaterialCoverJob.bc_id == old.bc_id,
            MaterialCoverJob.material_id == old.material_id,
            col(MaterialCoverJob.advertiser_id).in_(target_accounts),
            col(MaterialCoverJob.id).in_(dependent_jobs),
            MaterialCoverJob.purpose == "BUILD",
            MaterialCoverJob.status == "BLOCKED",
            MaterialCoverJob.error_code == "cover_source_unavailable",
            col(MaterialCoverJob.superseded_by_id).is_(None),
            col(MaterialCoverJob.share_batch_id).is_(None),
            col(MaterialCoverJob.request_armed_at).is_(None),
            col(MaterialCoverJob.known_image_id).is_(None),
            col(MaterialCoverJob.candidate_image_id).is_(None),
            col(MaterialCoverJob.claim_token).is_(None),
            col(MaterialCoverJob.dispatch_id).is_(None),
        )
        .order_by(col(MaterialCoverJob.id))
        .with_for_update()
    ).all() if old.purpose == "SOURCE" else []
    resumed = []
    for job in waiting:
        covers.request_cover_retry(session, context=context, job_id=job.id)
        for step in consumers:
            if (
                step.cover_job_id != job.id
                or step.status in {"SUCCEEDED", "RUNNING"}
                or step.remote_id
                or step.request_body is not None
                or step.lease_token is not None
                or step.dispatch_id is not None
            ):
                continue
            # 未发送 BUILD 使用正常 PENDING 等待；UNKNOWN 会退出搭建窗口，
            # 导致其未 armed 封面永远无法准入。成功 MATERIAL 保持原事实。
            step.status, step.phase, step.error_code = (
                "PENDING",
                "IDLE",
                "cover_pending",
            )
            step.updated_at = covers._now()
            evidence(
                session,
                step=step,
                claim=None,
                conclusion="MATERIAL_COVER_SOURCE_REISSUED",
                summary={
                    "cover_job_id": str(job.id),
                    "old_cover_job_id": str(old.id),
                    "new_cover_job_id": str(new.id),
                },
            )
        resumed.append(str(job.id))
    return {
        "old_cover_job_id": str(old.id),
        "new_cover_job_id": str(new.id),
        "rebound_material_step_ids": rebound,
        "resumed_build_cover_job_ids": resumed,
    }
