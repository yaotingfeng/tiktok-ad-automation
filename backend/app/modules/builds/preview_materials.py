"""预览、提交和最终发送共用已冻结素材排除规则，不重新推断素材状态。"""

from uuid import UUID

from sqlalchemy import SQLColumnExpression
from sqlmodel import col, select

from .preview_models import PreviewSkippedMaterial

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
