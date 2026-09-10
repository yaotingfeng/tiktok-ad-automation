"""Current authorized account evidence and ephemeral official source previews."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import sdk_client
from app.jobs.admission import AdmissionPolicy, admission_policy
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess

from . import sdk_assets as api
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


def source_info_policy(*, hard_limit: int) -> AdmissionPolicy:
    # Validate configured shared policy first. A nested INFO in a longer upload
    # process keeps every quota/key; only conservative orphan occupancy grows.
    policy = admission_policy(api.INFO_ENDPOINT)
    return policy.model_copy(
        update={"lease_ms": max(policy.lease_ms, (hard_limit + 10) * 1000)}
    )


def read_remote_source(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    source_asset_id: UUID,
    deadline: datetime,
    hard_limit: int,
    extend_lease: bool = False,
) -> api.SourcePreview:
    policy = (
        source_info_policy(hard_limit=hard_limit)
        if extend_lease
        else admission_policy(api.INFO_ENDPOINT)
    )
    budget = api.RemoteCallBudget(
        deadline=deadline, hard_limit_seconds=hard_limit, lease_ms=policy.lease_ms
    )
    budget.timeout(upload=False)
    with Session(database_engine) as db:
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
    with api.admitted_asset_call(
        redis_client,
        context=context,
        endpoint=api.INFO_ENDPOINT,
        advertiser_id=advertiser_id,
        policy=policy,
    ):
        with Session(database_engine) as db:
            current = resolve_remote_source(
                db,
                context=context,
                bc_id=bc_id,
                material_id=material_id,
                source_asset_id=source_asset_id,
            )
            if current is None or (current.video_id, current.connection_id) != (
                video_id,
                connection_id,
            ):
                raise DomainError(
                    "material_remote_source_unavailable", "来源证据已变化"
                )
            resolve_account_access(
                db,
                context=context,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                action="read",
            )
            # Exact usable grant was verified above. Resolving a newer preferred
            # connection never rewrites this mapping's recorded connection.
            with sdk_client(db, context=context, connection_id=connection_id) as client:
                db.commit()
                db.close()
                preview = api.read_source_preview(
                    client,
                    advertiser_id=advertiser_id,
                    video_id=video_id,
                    md5=md5,
                    allowed_hosts=settings.MATERIAL_REMOTE_MEDIA_HOSTS,
                    budget=budget,
                )
    with Session(database_engine) as db:
        current = resolve_remote_source(
            db,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=source_asset_id,
        )
        if current is None or (current.video_id, current.connection_id) != (
            video_id,
            connection_id,
        ):
            raise DomainError("material_remote_source_unavailable", "来源授权已变化")
    if preview.size != byte_size or datetime.now(UTC) >= deadline:
        raise DomainError(
            "material_preview_unverified", "来源视频规格或本次读取期限无法核实"
        )
    return preview
