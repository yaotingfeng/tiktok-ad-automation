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
    unit_id: UUID,
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
    """Read the immutable ad mapping, with a legacy group snapshot fallback.

    Previews frozen before the ad-level table existed have no rows there. Their
    group-level order remains the authoritative historical intent.
    """
    query = select(PreviewAdMaterial.material_id).where(
        PreviewAdMaterial.tenant_id == tenant_id,
        PreviewAdMaterial.preview_id == preview_id,
        PreviewAdMaterial.drama_id == drama_id,
        PreviewAdMaterial.group_no == group_no,
        PreviewAdMaterial.base_ad_no == base_ad_no,
    ).order_by(col(PreviewAdMaterial.position))
    ids = list(session.exec(query).all())
    # 只有整部剧尚未写入广告级映射时才兼容历史组级快照；新表部分缺行
    # 表示冻结数据损坏，必须返回空集合让执行链明确阻断。
    any_ad_rows = bool(
        session.exec(
            select(PreviewAdMaterial.material_id).where(
                PreviewAdMaterial.tenant_id == tenant_id,
                PreviewAdMaterial.preview_id == preview_id,
            )
        ).first()
    )
    if not ids and any_ad_rows:
        return []
    if not any_ad_rows:
        legacy = select(PreviewGroupMaterial.material_id).where(
            PreviewGroupMaterial.tenant_id == tenant_id,
            PreviewGroupMaterial.preview_id == preview_id,
            PreviewGroupMaterial.drama_id == drama_id,
            PreviewGroupMaterial.group_no == group_no,
        ).order_by(col(PreviewGroupMaterial.position))
        ids = list(session.exec(legacy).all())
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
