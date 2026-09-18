"""分发来源只取同租户授权库存；跨 BC 按可信内容身份匹配，不按名字猜素材。"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, or_
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import BCAccountAccess, TenantBC

from .models import AccountMaterial, MaterialFile, MaterialUploadAttempt


def distribution_sources(
    session: Session,
    *,
    context: TenantContext,
    materials: Sequence[MaterialFile],
    advertiser_id: str,
    route: FrozenTikTokRoute,
) -> dict[UUID, AccountMaterial]:
    if not materials:
        return {}
    # 原始文件仍属于自己的 BC；只借用内容完全相同的另一 BC 平台副本。
    ids = [m.id for m in materials]
    digests = [m.sha256 for m in materials if m.sha256 and m.digest_verified_at]
    readable = (
        usable_grants(
            tenant_id=context.tenant_id, bc_id=col(AccountMaterial.bc_id), action="read"
        )
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == AccountMaterial.connection_id,
        )
        .exists()
    )
    shareable = (
        usable_grants(tenant_id=context.tenant_id, bc_id=route.bc_id, action="upload")
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == route.connection_id,
        )
        .exists()
    )
    owned = (
        select(MaterialUploadAttempt.id)
        .where(
            MaterialUploadAttempt.tenant_id == AccountMaterial.tenant_id,
            MaterialUploadAttempt.bc_id == AccountMaterial.bc_id,
            MaterialUploadAttempt.material_id == AccountMaterial.material_id,
            MaterialUploadAttempt.advertiser_id == AccountMaterial.advertiser_id,
        )
        .exists()
    )
    primary = (
        select(TenantBC.material_advertiser_id)
        .where(TenantBC.tenant_id == context.tenant_id, TenantBC.bc_id == route.bc_id)
        .scalar_subquery()
    )
    rows = session.exec(
        select(AccountMaterial, MaterialFile)
        .join(
            MaterialFile,
            and_(
                col(AccountMaterial.material_id) == MaterialFile.id,
                col(AccountMaterial.tenant_id) == MaterialFile.tenant_id,
            ),
        )
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            or_(
                col(AccountMaterial.advertiser_id) != advertiser_id,
                col(AccountMaterial.bc_id) != route.bc_id,
            ),
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
            col(AccountMaterial.video_id) != "",
            readable,
            or_(col(AccountMaterial.bc_id) != route.bc_id, shareable),
            or_(
                col(MaterialFile.id).in_(ids),
                and_(
                    col(MaterialFile.sha256).in_(digests),
                    col(MaterialFile.digest_verified_at).is_not(None),
                ),
            ),
        )
        .order_by(
            (col(AccountMaterial.bc_id) == route.bc_id).desc(),
            # 转存后和原生同 BC 素材采用同一来源选择。优先固定主账户，
            # 不因某个下游副本刚回读完成就换来源、拆散共享矩形。
            (col(AccountMaterial.advertiser_id) == primary).desc().nulls_last(),
            owned.desc(),
            col(AccountMaterial.advertiser_id),
            col(AccountMaterial.id),
        )
    ).all()
    result = {}
    for material in materials:
        for source, source_file in rows:
            same = source.material_id == material.id
            matching = bool(
                material.digest_verified_at
                and source_file.digest_verified_at
                and material.sha256
                and material.video_md5
                and (material.sha256, material.video_md5, material.byte_size)
                == (source_file.sha256, source_file.video_md5, source_file.byte_size)
            )
            if same or matching:
                result[material.id] = source
                break
    return result
