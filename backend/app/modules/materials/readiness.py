"""Local preview evidence only: no SDK calls, admission leases, or queued work."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.jobs.admission import admission_policy
from app.jobs.celery_app import celery_app
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import freeze_route

from .channel_policy import require_url_upload
from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .remote_sources import require_remote_material
from .repository import asset_public, require_material_scope
from .routes import require_material_route, require_sdk_route
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


def require_execution_config(
    *, upload: bool, endpoint: str, original: bool = True, channel: str = "OFFICIAL_API"
) -> None:
    """Detect known deployment failures locally; runtime checks still enforce them."""
    if channel == "OFFICIAL_API":
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
    if upload and original:
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
    route: FrozenTikTokRoute | None = None,
) -> None:
    if route is not None:
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=material.bc_id,
            advertiser_id=advertiser_id,
            capability="upload",
        )
        require_sdk_route(route)
    if material.current_object_generation is not None:
        raise DomainError("material_result_pending", "请等待来源账户素材核实后继续")
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
        connection_id=route.connection_id if route else None,
    )
    require_execution_config(upload=True, endpoint="materials.upload_video_file")


def get_material_readiness_batch(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_ids: list[UUID],
    advertiser_id: str,
    route: FrozenTikTokRoute | None = None,
) -> dict[UUID, MaterialReadiness]:
    """Read one bounded group with current authority, without cross-call caching.

    All rows and authorization checks belong to this caller's short transaction.
    The single-material API delegates here so path priority cannot diverge.
    """
    if not material_ids or len(material_ids) > 50:
        raise ValueError("material readiness requires 1..50 materials")
    identities = set(material_ids)
    require_material_scope(session, context=context, bc_id=bc_id)
    materials = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == bc_id,
            col(MaterialFile.id).in_(identities),
        )
        .execution_options(populate_existing=True)
    ).all()
    if len(materials) != len(identities):
        raise DomainError("material_not_found", "未找到当前租户 BC 素材")

    def blocked(error: DomainError) -> MaterialReadiness:
        return MaterialReadiness(
            state="blocked",
            path="unavailable",
            reason_code=error.code,
            reason_message=error.message,
        )

    try:
        if route is None:
            # 独立页面的新只读入口冻结一次；已有任务直接核验原持久路线。
            route = freeze_route(session, context=context, bc_id=bc_id)
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            capability="build",
        )
    except DomainError as error:
        return {identity: blocked(error) for identity in identities}
    mappings = {
        row.material_id: row
        for row in session.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == bc_id,
                col(AccountMaterial.material_id).in_(identities),
                AccountMaterial.advertiser_id == advertiser_id,
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    # Partial unique index guarantees at most one unresolved operation per material.
    operations = {
        row.material_id: row
        for row in session.exec(
            select(MaterialAssetOperation)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.bc_id == bc_id,
                col(MaterialAssetOperation.material_id).in_(identities),
                MaterialAssetOperation.advertiser_id == advertiser_id,
                col(MaterialAssetOperation.status).in_(
                    ["sending", "verifying", "result_unknown"]
                ),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    unconfirmed = set(
        session.exec(
            select(MaterialAssetOperation.material_id)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.bc_id == bc_id,
                col(MaterialAssetOperation.material_id).in_(identities),
                MaterialAssetOperation.advertiser_id == advertiser_id,
                MaterialAssetOperation.path == "share_source",
                MaterialAssetOperation.status == "failed",
                col(MaterialAssetOperation.remote_response)[
                    "definite_no_effect"
                ].astext.is_distinct_from("true"),
            )
            .distinct()
        ).all()
    )
    grant = (
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(
            BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
            BCAccountAccess.connection_id == AccountMaterial.connection_id,
        )
        .exists()
    )
    legal_sources = set(
        session.exec(
            select(AccountMaterial.material_id)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == bc_id,
                col(AccountMaterial.material_id).in_(identities),
                AccountMaterial.advertiser_id != advertiser_id,
                AccountMaterial.status == "available",
                col(AccountMaterial.verified_at).is_not(None),
                col(AccountMaterial.video_id) != "",
                grant,
            )
            .distinct()
        ).all()
    )
    # Deployment policy and the upload permission are identical for this bounded
    # group. Check lazily so existing verified target assets keep their priority.
    authority_checked = False
    authority_error: DomainError | None = None

    def require_target_upload() -> None:
        nonlocal authority_checked, authority_error
        # 只在这一短事务、同一目标route/账户中共享；来源连接的权限不借用此结果。
        if not authority_checked:
            try:
                require_material_route(
                    session,
                    context=context,
                    route=route,
                    bc_id=bc_id,
                    advertiser_id=advertiser_id,
                    capability="upload",
                )
            except DomainError as error:
                authority_error = error
            authority_checked = True
        if authority_error:
            raise authority_error

    upload_checked = False
    upload_error: DomainError | None = None
    result: dict[UUID, MaterialReadiness] = {}
    for material in materials:
        mapping = mappings.get(material.id)
        operation = operations.get(material.id)
        try:
            if mapping_fresh(mapping):
                assert mapping
                result[material.id] = MaterialReadiness(
                    state="ready", path="existing_target", mapping=asset_public(mapping)
                )
                continue
            if (mapping and mapping.video_id.strip()) or operation:
                if not material.video_md5 or len(material.video_md5) != 32:
                    raise DomainError(
                        "material_digest_missing", "素材缺少可核实内容摘要"
                    )
                endpoint = (
                    "materials.get_videos"
                    if mapping
                    or (operation and operation.remote_response.get("video_id"))
                    else "materials.search_videos"
                )
                require_execution_config(
                    upload=False, endpoint=endpoint, channel=route.channel
                )
                result[material.id] = MaterialReadiness(
                    state="preparable",
                    path="existing_target",
                    mapping=asset_public(mapping) if mapping else None,
                )
                continue
            if material.id in unconfirmed:
                raise DomainError(
                    "material_share_unconfirmed",
                    "共享尚无明确未生效证据，需要先核实结果",
                )
            if material.id in legal_sources and settings.MATERIAL_REMOTE_MEDIA_HOSTS:
                require_remote_material(material)
                if material.byte_size > settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
                    raise DomainError(
                        "url_upload_capacity_exceeded",
                        "素材超过当前URL转存工程容量限制",
                    )
                require_target_upload()
                if route.channel == "OFFICIAL_MCP":
                    require_url_upload(
                        material_upload_policy(
                            channel=route.channel,
                            adapter_contract_revision=route.adapter_contract_revision,
                        ),
                        byte_size=material.byte_size,
                    )
                require_execution_config(
                    upload=True,
                    endpoint="materials.upload_video_url",
                    original=False,
                    channel=route.channel,
                )
                require_execution_config(
                    upload=False, endpoint="materials.get_videos", channel=route.channel
                )
                result[material.id] = MaterialReadiness(
                    state="preparable", path="share_source"
                )
                continue
            if material.storage_state == "stored":
                if material.current_object_generation is not None:
                    raise DomainError(
                        "material_preview_unverified"
                        if material.id in legal_sources
                        else "material_result_pending",
                        "需配置已核实的视频域名后才能分发"
                        if material.id in legal_sources
                        else "请等待来源账户素材核实后继续",
                    )
                if material.byte_size > settings.MATERIAL_SDK_MAX_UPLOAD_BYTES:
                    raise DomainError(
                        "sdk_upload_capacity_exceeded",
                        "原文件超过当前平台上传内存容量边界",
                    )
                if not upload_checked:
                    try:
                        require_sdk_route(route)
                        require_target_upload()
                        require_execution_config(
                            upload=True, endpoint="materials.upload_video_file"
                        )
                    except DomainError as error:
                        upload_error = error
                    upload_checked = True
                if upload_error:
                    raise upload_error
                result[material.id] = MaterialReadiness(
                    state="preparable", path="upload_original"
                )
                continue
            code = (
                "material_share_unverified"
                if material.id in legal_sources
                else "original_unavailable"
            )
            if material.current_object_generation is not None:
                raise DomainError(
                    "material_preview_unverified"
                    if material.id in legal_sources
                    else "material_remote_source_unavailable",
                    "需配置已核实的视频域名后才能分发"
                    if material.id in legal_sources
                    else "没有可用来源，请恢复账户授权或重新上传原文件",
                )
            raise DomainError(code, "没有已核实的原生共享路径或完整原文件")
        except DomainError as error:
            result[material.id] = blocked(error)
    return result


def get_material_readiness(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    route: FrozenTikTokRoute | None = None,
) -> MaterialReadiness:
    return get_material_readiness_batch(
        session,
        context=context,
        bc_id=bc_id,
        material_ids=[material_id],
        advertiser_id=advertiser_id,
        route=route,
    )[material_id]
