"""搭建失败现场的有界白名单快照；不记录正文、授权或素材下载地址。"""

from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.materials.cover_models import MaterialCoverJob

from .execution_models import ExecutionStep


def _cover_snapshot(job: MaterialCoverJob) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "material_id": str(job.material_id),
        "advertiser_id": job.advertiser_id,
        "purpose": job.purpose,
        "status": job.status,
        "error_code": job.error_code,
        "video_id": job.video_id,
        "request_armed": job.request_armed_at is not None,
        "has_image_id": job.known_image_id is not None,
        "has_candidate": job.candidate_image_id is not None,
        "search_page": job.next_page,
        "search_total": job.search_total,
        "search_ambiguous": job.search_ambiguous,
        "failure_count": job.failure_count,
        "updated_at": job.updated_at.isoformat(),
    }


def cover_failure_context(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    advertiser_id: str,
    connection_id: UUID,
    material_ids: list[UUID],
) -> dict[str, Any]:
    # 只截取本次冻结素材的异常目标。来源和目标分开保存，防止共享后的
    # 目标图片被误当成原上传来源；上限控制单条失败证据大小。
    targets = session.exec(
        select(MaterialCoverJob)
        .where(
            MaterialCoverJob.tenant_id == tenant_id,
            MaterialCoverJob.bc_id == bc_id,
            MaterialCoverJob.advertiser_id == advertiser_id,
            MaterialCoverJob.connection_id == connection_id,
            col(MaterialCoverJob.material_id).in_(material_ids),
            col(MaterialCoverJob.superseded_by_id).is_(None),
            MaterialCoverJob.status != "READY",
        )
        .order_by(col(MaterialCoverJob.id))
        .limit(21)
    ).all()
    sources = (
        session.exec(
            select(MaterialCoverJob)
            .where(
                MaterialCoverJob.tenant_id == tenant_id,
                MaterialCoverJob.bc_id == bc_id,
                col(MaterialCoverJob.material_id).in_(
                    [j.material_id for j in targets[:20]]
                ),
                MaterialCoverJob.purpose == "SOURCE",
                col(MaterialCoverJob.superseded_by_id).is_(None),
            )
            .order_by(col(MaterialCoverJob.id))
            .limit(41)
        ).all()
        if targets
        else []
    )
    return {
        "targets": [_cover_snapshot(j) for j in targets[:20]],
        "sources": [_cover_snapshot(j) for j in sources[:40]],
        "truncated": len(targets) > 20 or len(sources) > 40,
    }


def failure_context(session: Session, step: ExecutionStep) -> dict[str, Any]:
    from .preview_models import BuildUnit, PreviewDrama

    result: dict[str, Any] = {
        "bc_id": step.bc_id,
        "unit_id": str(step.unit_id),
        "kind": step.kind,
        "phase": step.phase,
        "request_armed": step.request_body is not None,
        "body_digest": step.request_body_digest,
    }
    unit = session.get(BuildUnit, step.unit_id)
    if unit is None or unit.tenant_id != step.tenant_id:
        return result
    result["advertiser_id"] = unit.advertiser_id
    result["connection_id"] = str(unit.connection_id)
    drama = session.exec(
        select(PreviewDrama).where(
            PreviewDrama.tenant_id == step.tenant_id,
            PreviewDrama.preview_id == step.preview_id,
            PreviewDrama.drama_id == unit.drama_id,
        )
    ).one_or_none()
    if drama:
        result["external_drama_id"] = drama.external_drama_id
        result["provider"] = drama.provider_pinyin
        result["landing_url_sha256"] = sha256(drama.url.encode()).hexdigest()
    if step.parent_step_id:
        parent = session.get(ExecutionStep, step.parent_step_id)
        if parent and parent.tenant_id == step.tenant_id:
            result["parent_step_id"] = str(parent.id)
            result["minis_id"] = (parent.request_body or {}).get("minis_id")
    if (step.error_code or "").startswith("cover_"):
        # MATERIAL步骤保存本组合冻结素材集合，不能从可变剧单重新推导。
        material_ids = list(
            session.exec(
                select(ExecutionStep.material_id).where(
                    ExecutionStep.tenant_id == step.tenant_id,
                    ExecutionStep.submission_id == step.submission_id,
                    ExecutionStep.unit_id == step.unit_id,
                    ExecutionStep.kind == "MATERIAL",
                    col(ExecutionStep.material_id).is_not(None),
                )
            ).all()
        )
        result["covers"] = cover_failure_context(
            session,
            tenant_id=step.tenant_id,
            bc_id=step.bc_id,
            advertiser_id=unit.advertiser_id,
            connection_id=unit.connection_id,
            material_ids=[m for m in material_ids if m is not None],
        )
    return result
