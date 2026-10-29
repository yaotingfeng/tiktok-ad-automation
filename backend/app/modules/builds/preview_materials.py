"""预览、提交和最终发送共用已冻结素材排除规则，不重新推断素材状态。"""

from uuid import UUID

from sqlalchemy import SQLColumnExpression
from sqlmodel import col, select

from .preview_models import (
    PreviewAdMaterial,
    PreviewGroupMaterial,
    PreviewSkippedMaterial,
)

# 只隔离素材自身的问题。授权、账户、通道配置或场景错误仍阻断组合。
SKIPPABLE_MATERIAL_REASONS = frozenset(
    {
        "material_remote_source_unavailable",
        "original_unavailable",
        "material_digest_missing",
        "material_result_pending",
        "material_share_unconfirmed",
        "material_share_unverified",
        "material_preview_unverified",
        "url_upload_capacity_exceeded",
        "sdk_upload_capacity_exceeded",
    }
)


def material_limit_exceeded(counts: list[int] | tuple[int, ...], maximum: int) -> bool:
    """冻结场景提供每广告素材上限；不把平台默认值写死在预览层。"""
    return any(count > maximum for count in counts)


def material_not_skipped(
    *,
    tenant_id: UUID,
    unit_id: UUID | SQLColumnExpression[UUID],
    material_id: SQLColumnExpression[UUID],
) -> SQLColumnExpression[bool]:
    return (
        ~select(PreviewSkippedMaterial.material_id)
        .where(
            PreviewSkippedMaterial.tenant_id == tenant_id,
            PreviewSkippedMaterial.unit_id == unit_id,
            col(PreviewSkippedMaterial.material_id) == material_id,
        )
        .exists()
    )


def frozen_ad_material_ids(
    session,
    *,
    tenant_id: UUID,
    preview_id: UUID,
    drama_id: UUID,
    group_no: int,
    base_ad_no: int,
    unit_id: UUID | None = None,
) -> list[UUID]:
    from .creative_units import frozen_ad_creative_units, preview_contract

    contract = preview_contract(session, tenant_id=tenant_id, preview_id=preview_id)
    if contract.version == 2:
        units = frozen_ad_creative_units(
            session,
            tenant_id=tenant_id,
            preview_id=preview_id,
            drama_id=drama_id,
            group_no=group_no,
            base_ad_no=base_ad_no,
        )
        ids = [member.material_id for unit in units for member in unit.members]
        if contract.media_kind == "CAROUSEL":
            return ids
    else:
        if contract.legacy_material_layout not in {"GROUP", "AD"}:
            return []
        model = (
            PreviewAdMaterial
            if contract.legacy_material_layout == "AD"
            else PreviewGroupMaterial
        )
        query = select(model.material_id).where(
            model.tenant_id == tenant_id,
            model.preview_id == preview_id,
            model.drama_id == drama_id,
            model.group_no == group_no,
        )
        if contract.legacy_material_layout == "AD":
            query = query.where(PreviewAdMaterial.base_ad_no == base_ad_no)
        ids = list(session.exec(query.order_by(col(model.position))).all())
    if unit_id is None:
        return ids
    if not ids:
        return ids
    # 当前广告的素材一次性读取排除证据，避免每个素材单独查询跳过表。
    skipped = set(
        session.exec(
            select(PreviewSkippedMaterial.material_id).where(
                PreviewSkippedMaterial.tenant_id == tenant_id,
                PreviewSkippedMaterial.unit_id == unit_id,
                col(PreviewSkippedMaterial.material_id).in_(ids),
            )
        ).all()
    )
    return [material_id for material_id in ids if material_id not in skipped]
