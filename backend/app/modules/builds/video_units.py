"""视频草稿只有创意单元这一份现行事实，文件身份仅保留在成员行。"""

import json
from hashlib import sha256
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.materials.models import MaterialFile

from .image_composition_models import DraftCreativeUnit, DraftCreativeUnitItem
from .models import BuildDraft

VIDEO_GENERATION = 1


def video_content_digest(material_id: UUID, recorded_sha256: str | None) -> str:
    # 单元摘要记录成员身份和已记录内容摘要；null 明确保留未知字节，不能冒充文件 hash。
    value = json.dumps([str(material_id), recorded_sha256], ensure_ascii=False)
    return sha256(value.encode()).hexdigest()


def add_video_unit(
    session: Session,
    draft: BuildDraft,
    *,
    drama_id: UUID,
    material_id: UUID,
    position: int,
) -> DraftCreativeUnit:
    material = session.get(MaterialFile, material_id)
    if (
        material is None
        or material.tenant_id != draft.tenant_id
        or material.media_type != "VIDEO"
    ):
        raise DomainError("material_not_found", "素材不在当前租户视频库")
    unit = DraftCreativeUnit(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        bc_id=draft.bc_id,
        drama_id=drama_id,
        generation=VIDEO_GENERATION,
        position=position,
        kind="VIDEO",
        music_status="NOT_REQUIRED",
        content_digest=video_content_digest(material_id, material.sha256),
    )
    session.add(unit)
    session.flush()
    session.add(
        DraftCreativeUnitItem(
            tenant_id=draft.tenant_id,
            draft_id=draft.id,
            bc_id=draft.bc_id,
            drama_id=drama_id,
            generation=VIDEO_GENERATION,
            unit_id=unit.id,
            material_id=material_id,
            position=1,
            kind="VIDEO",
            media_type="VIDEO",
        )
    )
    return unit


def delete_video_units(
    session: Session, draft: BuildDraft, *, drama_id: UUID | None = None
) -> None:
    for model in (DraftCreativeUnitItem, DraftCreativeUnit):
        query = delete(model).where(
            col(model.tenant_id) == draft.tenant_id,
            col(model.draft_id) == draft.id,
            col(model.kind) == "VIDEO",
        )
        if drama_id is not None:
            query = query.where(col(model.drama_id) == drama_id)
        SASession.execute(session, query)


def ordered_units(session: Session, draft: BuildDraft, *, drama_id: UUID):
    generation = (
        VIDEO_GENERATION
        if draft.media_kind == "VIDEO"
        else draft.active_image_generation
    )
    return session.exec(
        select(DraftCreativeUnit)
        .where(
            DraftCreativeUnit.tenant_id == draft.tenant_id,
            DraftCreativeUnit.draft_id == draft.id,
            DraftCreativeUnit.drama_id == drama_id,
            DraftCreativeUnit.kind == draft.media_kind,
            DraftCreativeUnit.generation == generation,
        )
        .order_by(col(DraftCreativeUnit.position))
    ).all()


def video_material_ids(
    session: Session, draft: BuildDraft, *, drama_id: UUID
) -> dict[UUID, UUID]:
    # 仅供 P3-T1 过渡视频冻结器显式解引用；不能将轮播写入旧视频冻结合同。
    if draft.media_kind != "VIDEO":
        raise DomainError("preview_contract_unavailable", "图片预览需要 V2 冻结合同")
    rows = session.exec(
        select(DraftCreativeUnitItem).where(
            DraftCreativeUnitItem.tenant_id == draft.tenant_id,
            DraftCreativeUnitItem.draft_id == draft.id,
            DraftCreativeUnitItem.drama_id == drama_id,
            DraftCreativeUnitItem.generation == VIDEO_GENERATION,
            DraftCreativeUnitItem.kind == "VIDEO",
        )
    ).all()
    return {row.unit_id: row.material_id for row in rows}
