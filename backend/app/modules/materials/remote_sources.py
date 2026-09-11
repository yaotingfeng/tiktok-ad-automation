"""Current authorized account evidence and ephemeral official source previews."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.admission import admission_policy
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import freeze_route, verify_route

from .models import AccountMaterial, MaterialFile
from .repository import require_material_scope


def legal_source_grant(*, context: TenantContext, bc_id: str) -> Any:
    return (
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == AccountMaterial.connection_id,
        )
        .exists()
    )


def resolve_remote_source(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    target_advertiser_id: str | None = None,
    source_asset_id: UUID | None = None,
) -> AccountMaterial | None:
    """Select from all current legal mappings; never infer authority from VID/MID."""
    require_material_scope(session, context=context, bc_id=bc_id)
    query = select(AccountMaterial).where(
        AccountMaterial.tenant_id == context.tenant_id,
        AccountMaterial.bc_id == bc_id,
        AccountMaterial.material_id == material_id,
        AccountMaterial.status == "available",
        col(AccountMaterial.verified_at).is_not(None),
        col(AccountMaterial.video_id) != "",
        legal_source_grant(context=context, bc_id=bc_id),
    )
    if target_advertiser_id is not None:
        query = query.where(AccountMaterial.advertiser_id != target_advertiser_id)
    if source_asset_id is not None:
        query = query.where(AccountMaterial.id == source_asset_id)
    return session.exec(
        query.order_by(col(AccountMaterial.verified_at).desc(), col(AccountMaterial.id))
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()


def require_remote_material(material: MaterialFile) -> None:
    if (
        not material.video_md5
        or len(material.video_md5) != 32
        or (
            material.current_object_generation is not None
            and not material.digest_verified_at
        )
    ):
        raise DomainError("material_digest_missing", "素材缺少可信内容摘要")
    if not settings.MATERIAL_REMOTE_MEDIA_HOSTS:
        raise DomainError("material_preview_unverified", "尚未配置已核实的视频域名")


def read_frozen_remote_source(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    source_asset_id: UUID,
    route: FrozenTikTokRoute,
    deadline: datetime,
) -> material_types.SourcePreview:
    from .source_uploads import READ_HARD_LIMIT

    # 预览是独立短读取；它不能继承并扩张外层长上传期限或租约。
    deadline = min(deadline, datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5))
    policy = admission_policy("materials.get_videos")
    budget = material_types.RemoteCallBudget(
        deadline=deadline, hard_limit_seconds=READ_HARD_LIMIT, lease_ms=policy.lease_ms
    )
    budget.timeout(upload=False)
    with bounded_session(database_engine, task_deadline=deadline) as db:
        source = resolve_remote_source(
            db,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=source_asset_id,
        )
        material = db.get(MaterialFile, material_id)
        if (
            source is None
            or material is None
            or material.tenant_id != context.tenant_id
        ):
            raise DomainError(
                "material_remote_source_unavailable", "来源授权已变化，请恢复授权或补传"
            )
        require_remote_material(material)
        advertiser_id, video_id, connection_id, md5, byte_size = (
            source.advertiser_id,
            source.video_id,
            source.connection_id,
            material.video_md5,
            material.byte_size,
        )
        assert md5
    with bounded_session(database_engine, task_deadline=deadline) as db:
        current = resolve_remote_source(
            db,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=source_asset_id,
        )
        if current is None or (
            current.advertiser_id,
            current.video_id,
            current.connection_id,
        ) != (
            advertiser_id,
            video_id,
            connection_id,
        ):
            raise DomainError("material_remote_source_unavailable", "来源证据已变化")
        resolve_account_access(
            db,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            action="read",
            connection_id=connection_id,
        )
        if (route.tenant_id, route.bc_id, route.connection_id) != (
            context.tenant_id,
            bc_id,
            connection_id,
        ):
            raise DomainError("frozen_route_scope_mismatch", "来源与冻结连接范围不一致")
        verify_route(
            db,
            context=context,
            route=route,
            advertiser_id=advertiser_id,
            capability="read",
        )
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        preview = gateway.materials.read_source_preview(
            advertiser_id=advertiser_id, video_id=video_id, budget=budget
        )
    with bounded_session(database_engine, task_deadline=deadline) as db:
        # 返回临时预览能力前重新核验原授权；凭据轮转不改变冻结授权语义。
        verify_route(
            db,
            context=context,
            route=route,
            advertiser_id=advertiser_id,
            capability="read",
        )
        current = resolve_remote_source(
            db,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=source_asset_id,
        )
        if current is None or (
            current.advertiser_id,
            current.video_id,
            current.connection_id,
        ) != (
            advertiser_id,
            video_id,
            connection_id,
        ):
            raise DomainError("material_remote_source_unavailable", "来源授权已变化")
        # HTTP 期间持久内容身份也可能变化；旧快照不能证明当前素材。
        material = db.get(MaterialFile, material_id)
        if material is None or (
            material.tenant_id,
            material.bc_id,
            material.video_md5,
            material.byte_size,
        ) != (context.tenant_id, bc_id, md5, byte_size):
            raise DomainError("material_preview_unverified", "持久素材身份已变化")
        require_remote_material(material)
    if (preview.advertiser_id, preview.video_id, preview.md5, preview.size) != (
        advertiser_id,
        video_id,
        md5.lower(),
        byte_size,
    ) or datetime.now(UTC) >= deadline:
        raise DomainError(
            "material_preview_unverified", "来源视频规格或本次读取期限无法核实"
        )
    return preview


def read_remote_source(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    source_asset_id: UUID,
    deadline: datetime,
) -> material_types.SourcePreview:
    """页面新预览独立冻结；已有任务必须调用 required-route 的内部边界。"""
    from .source_uploads import READ_HARD_LIMIT

    deadline = min(deadline, datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5))
    material_types.RemoteCallBudget(
        deadline=deadline,
        hard_limit_seconds=READ_HARD_LIMIT,
        lease_ms=admission_policy("materials.get_videos").lease_ms,
    ).timeout(upload=False)
    with bounded_session(database_engine, task_deadline=deadline) as db:
        source = resolve_remote_source(
            db,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=source_asset_id,
        )
        if source is None:
            raise DomainError(
                "material_remote_source_unavailable", "来源证据或权限已改变"
            )
        route = freeze_route(
            db, context=context, bc_id=bc_id, connection_id=source.connection_id
        )
    return read_frozen_remote_source(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        source_asset_id=source_asset_id,
        route=route,
        deadline=deadline,
    )
