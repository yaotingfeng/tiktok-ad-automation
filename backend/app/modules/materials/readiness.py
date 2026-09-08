"""Local preview evidence only: no SDK calls, admission leases, or queued work."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.admission import admission_policy
from app.jobs.celery_app import celery_app
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess

from . import sdk_assets as api
from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .repository import asset_public, require_material_scope
from .schemas import MaterialReadiness
from .source_uploads import READ_HARD_LIMIT, UPLOAD_HARD_LIMIT


def choose_material_path(
    *,
    target_verified: bool,
    target_known: bool,
    shareable_source: bool,
    original_available: bool,
) -> tuple[str, str]:
    if target_verified:
        return "ready", "existing_target"
    if target_known:
        return "preparable", "existing_target"
    if shareable_source:
        return "preparable", "share_source"
    if original_available:
        return "preparable", "upload_original"
    return "blocked", "unavailable"


def load_material(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    lock: bool = False,
) -> MaterialFile:
    require_material_scope(session, context=context, bc_id=bc_id)
    statement = (
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == bc_id,
            MaterialFile.id == material_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    material = session.exec(statement).first()
    if material is None:
        raise DomainError("material_not_found", "未找到当前租户 BC 素材")
    return material


def target_mapping(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
) -> AccountMaterial | None:
    return session.exec(
        select(AccountMaterial)
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == bc_id,
            AccountMaterial.material_id == material_id,
            AccountMaterial.advertiser_id == advertiser_id,
        )
        .execution_options(populate_existing=True)
    ).first()


def mapping_fresh(asset: AccountMaterial | None) -> bool:
    return bool(
        asset
        and asset.status == "available"
        and asset.video_id.strip()
        and asset.verified_at
        and asset.verified_at
        >= datetime.now(UTC)
        - timedelta(seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS)
    )


def has_legal_source_mid(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
) -> bool:
    # SQL EXISTS over the actual inventory, not MaterialCandidate's one-row hint.
    grant = (
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == AccountMaterial.connection_id,
        )
        .exists()
    )
    return (
        session.exec(
            select(AccountMaterial.id)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == bc_id,
                AccountMaterial.material_id == material_id,
                AccountMaterial.advertiser_id != advertiser_id,
                AccountMaterial.status == "available",
                col(AccountMaterial.verified_at).is_not(None),
                col(AccountMaterial.mid).is_not(None),
                col(AccountMaterial.mid) != "",
                grant,
            )
            .limit(1)
        ).first()
        is not None
    )


def require_execution_config(*, upload: bool, endpoint: str) -> None:
    """Detect known deployment failures locally; runtime checks still enforce them."""
    settings.require_tiktok_app()
    settings.require_connection_encryption()
    if celery_app.conf.task_always_eager or celery_app.conf.worker_pool != "prefork":
        raise DomainError(
            "material_worker_unbounded", "素材准备需要 prefork 后台工作进程"
        )
    policy = admission_policy(endpoint)
    hard_limit = UPLOAD_HARD_LIMIT if upload else READ_HARD_LIMIT
    if policy.lease_ms <= (hard_limit + 5) * 1000:
        raise DomainError(
            "admission_policy_invalid", "素材调用租约必须长于工作进程硬限"
        )
    if upload:
        settings.require_object_storage()
        if policy.endpoint_max_inflight > settings.MATERIAL_SDK_UPLOAD_MAX_INFLIGHT:
            raise DomainError(
                "admission_policy_invalid", "素材上传并发超过内存容量边界"
            )


def require_upload_path(
    session: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    advertiser_id: str,
) -> None:
    if material.storage_state != "stored":
        raise DomainError("original_unavailable", "没有可用的完整原文件")
    if material.byte_size > settings.MATERIAL_SDK_MAX_UPLOAD_BYTES:
        raise DomainError(
            "sdk_upload_capacity_exceeded", "原文件超过当前平台上传内存容量边界"
        )
    resolve_account_access(
        session,
        context=context,
        bc_id=material.bc_id,
        advertiser_id=advertiser_id,
        action="upload",
    )
    require_execution_config(upload=True, endpoint=api.UPLOAD_ENDPOINT)


def get_material_readiness(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
) -> MaterialReadiness:
    material = load_material(
        session, context=context, bc_id=bc_id, material_id=material_id
    )
    try:
        resolve_account_access(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            action="build",
        )
        mapping = target_mapping(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            advertiser_id=advertiser_id,
        )
        if mapping_fresh(mapping):
            assert mapping
            return MaterialReadiness(
                state="ready", path="existing_target", mapping=asset_public(mapping)
            )
        operation = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.bc_id == bc_id,
                MaterialAssetOperation.material_id == material_id,
                MaterialAssetOperation.advertiser_id == advertiser_id,
                col(MaterialAssetOperation.status).in_(
                    ["sending", "verifying", "result_unknown"]
                ),
            )
        ).first()
        if (mapping and mapping.video_id.strip()) or operation:
            if not material.video_md5 or len(material.video_md5) != 32:
                raise DomainError("material_digest_missing", "素材缺少可核实内容摘要")
            endpoint = (
                api.INFO_ENDPOINT
                if mapping or (operation and operation.remote_response.get("video_id"))
                else api.SEARCH_ENDPOINT
            )
            require_execution_config(upload=False, endpoint=endpoint)
            return MaterialReadiness(
                state="preparable",
                path="existing_target",
                mapping=asset_public(mapping) if mapping else None,
            )
        unconfirmed_share = session.exec(
            select(MaterialAssetOperation.id)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.material_id == material_id,
                MaterialAssetOperation.advertiser_id == advertiser_id,
                MaterialAssetOperation.path == "share_source",
                MaterialAssetOperation.status == "failed",
                col(MaterialAssetOperation.remote_response)[
                    "definite_no_effect"
                ].astext.is_distinct_from("true"),
            )
            .limit(1)
        ).first()
        if unconfirmed_share is not None:
            raise DomainError(
                "material_share_unconfirmed", "共享尚无明确未生效证据，需要先核实结果"
            )
        # No live contract/permission evidence currently establishes cross-account
        # sharing. A source MID alone cannot turn this flag on.
        legal_source = has_legal_source_mid(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            advertiser_id=advertiser_id,
        )
        if material.storage_state == "stored":
            require_upload_path(
                session, context=context, material=material, advertiser_id=advertiser_id
            )
            return MaterialReadiness(state="preparable", path="upload_original")
        code = "material_share_unverified" if legal_source else "original_unavailable"
        raise DomainError(code, "没有已核实的原生共享路径或完整原文件")
    except DomainError as error:
        return MaterialReadiness(
            state="blocked",
            path="unavailable",
            reason_code=error.code,
            reason_message=error.message,
        )
