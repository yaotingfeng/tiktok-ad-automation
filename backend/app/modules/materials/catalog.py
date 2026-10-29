"""Bounded upload history and authorized read-only original capabilities."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page, count_rows
from app.integrations.tiktok.bounded_resources import bounded_session
from app.modules.tenants.permissions import require_tenant

from .models import AccountMaterial, MaterialFile, ObjectUpload, UploadBatch
from .repository import decode_material_cursor, encode_material_cursor
from .schemas import (
    RemoteMaterialPreview,
    SignedPreview,
    UploadBatchResult,
    UploadBatchSummary,
    UploadStage,
)
from .storage import object_key_for, sign_original_preview, storage_error
from .uploads import get_upload_batch, require_bc


def remote_preview(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    material_id: UUID,
    bc_id: str | None = None,
) -> RemoteMaterialPreview:
    """Read-only fixed-source gateway with a bounded child budget.

    This read never changes readiness, queues material work or issues an
    original-use permission. A short MCP token may queue credential refresh;
    sources and authority are verified before returning the preview.
    """
    from app.modules.accounts.access import usable_grants
    from app.modules.accounts.models import BCAccountAccess

    from .remote_sources import read_remote_source
    from .source_uploads import READ_HARD_LIMIT

    deadline = datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
    with bounded_session(database_engine, task_deadline=deadline) as db:
        require_tenant(
            db, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
        )
        material = db.exec(
            select(MaterialFile).where(
                MaterialFile.tenant_id == context.tenant_id,
                MaterialFile.id == material_id,
            )
        ).one_or_none()
        if material is None:
            raise storage_error("material_not_found")
        if bc_id is not None:
            require_bc(db, context=context, bc_id=bc_id, action="read")
        # 草稿缩略图只读指定 BC 的真实副本；素材库未指定时仍可读租户合法来源。
        # 授权与实际 BC 在 SQL 中关联，不能拿原上传 BC 代替当前草稿 BC。
        grant = (
            usable_grants(
                tenant_id=context.tenant_id,
                bc_id=col(AccountMaterial.bc_id),
                action="read",
            )
            .where(
                BCAccountAccess.advertiser_id == AccountMaterial.advertiser_id,
                BCAccountAccess.connection_id == AccountMaterial.connection_id,
            )
            .exists()
        )
        source = db.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.material_id == material_id,
                AccountMaterial.status == "available",
                col(AccountMaterial.verified_at).is_not(None),
                or_(
                    col(AccountMaterial.video_id) != "",
                    (col(AccountMaterial.media_type) == "IMAGE")
                    & (col(AccountMaterial.is_carousel_usable).is_(True)),
                ),
                grant,
                *([AccountMaterial.bc_id == bc_id] if bc_id is not None else []),
            )
            .order_by(
                case((col(AccountMaterial.bc_id) == material.bc_id, 0), else_=1),
                col(AccountMaterial.verified_at).desc(),
                col(AccountMaterial.id),
            )
            .limit(1)
        ).first()
        if source is None:
            raise DomainError(
                "material_remote_source_unavailable", "暂无法预览，请恢复来源授权或补传"
            )
        source_id = source.id
        bc_id = source.bc_id
        if material.media_type == "IMAGE":
            from app.modules.accounts.routing import freeze_route, verify_route

            from .image_distribution import image_ready

            route = freeze_route(
                db, context=context, bc_id=bc_id, connection_id=source.connection_id
            )
            if not image_ready(source, route):
                raise DomainError(
                    "image_source_unavailable", "图片授权代数已改变，请重新核实"
                )
            image_id, account = source.image_id, source.advertiser_id
            content_identity = (
                material.tenant_id,
                material.bc_id,
                material.media_type,
                material.sha256,
                material.video_md5,
                material.byte_size,
                material.width,
                material.height,
            )
            source_identity = (
                source.tenant_id,
                source.bc_id,
                source.material_id,
                source.advertiser_id,
                source.image_id,
                source.connection_id,
                source.mid,
                source.verified_at,
            )
        else:
            image_id = None
    if image_id:
        from app.integrations.tiktok.contracts.materials import RemoteCallBudget
        from app.integrations.tiktok.gateway import open_tiktok_gateway
        from app.jobs.admission import admission_policy

        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
        ) as gateway:
            image = gateway.materials.read_image_preview(
                advertiser_id=account,
                image_id=image_id,
                budget=RemoteCallBudget(
                    deadline,
                    READ_HARD_LIMIT,
                    admission_policy(
                        "materials.get_images", channel=route.channel
                    ).lease_ms,
                ),
            )
        with bounded_session(database_engine, task_deadline=deadline) as db:
            # 签名 URL 是临时读取能力；返回前拒绝请求期间撤销的授权或替换的来源。
            verify_route(
                db,
                context=context,
                route=route,
                advertiser_id=account,
                capability="read",
            )
            current = db.get(AccountMaterial, source_id)
            if (
                not image_ready(current, route)
                or current is None
                or (
                    current.tenant_id,
                    current.bc_id,
                    current.material_id,
                    current.advertiser_id,
                    current.image_id,
                    current.connection_id,
                    current.mid,
                    current.verified_at,
                )
                != source_identity
            ):
                raise DomainError("image_source_unavailable", "图片来源证据已变化")
            material = db.get(MaterialFile, material_id)
            if (
                material is None
                or (
                    material.tenant_id,
                    material.bc_id,
                    material.media_type,
                    material.sha256,
                    material.video_md5,
                    material.byte_size,
                    material.width,
                    material.height,
                )
                != content_identity
            ):
                raise DomainError("image_preview_unavailable", "图片内容身份已变化")
        if (image.advertiser_id, image.image_id) != (account, image_id) or datetime.now(
            UTC
        ) >= deadline:
            raise DomainError(
                "image_preview_unavailable", "图片预览身份或读取期限无法核实"
            )
        return RemoteMaterialPreview(
            bc_id=bc_id,
            advertiser_id=account,
            media_type="IMAGE",
            image_id=image.image_id,
            url=image.image_url,
            width=image.width,
            height=image.height,
            format="image",
        )
    preview = read_remote_source(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        source_asset_id=source_id,
        deadline=deadline,
    )
    return RemoteMaterialPreview(
        bc_id=bc_id,
        url=preview.url,
        advertiser_id=preview.advertiser_id,
        video_id=preview.video_id,
        width=preview.width,
        height=preview.height,
        duration=preview.duration,
        format=preview.format,
    )


def find_upload_request(
    session: Session, *, context: TenantContext, bc_id: str, request_id: UUID
) -> UploadBatchResult:
    require_bc(session, context=context, bc_id=bc_id, action="read")
    identity = session.exec(
        select(UploadBatch.id).where(
            UploadBatch.tenant_id == context.tenant_id,
            UploadBatch.bc_id == bc_id,
            UploadBatch.request_id == request_id,
        )
    ).one_or_none()
    if identity is None:
        raise storage_error("upload_batch_not_found")
    return get_upload_batch(session, context=context, batch_id=identity)


def upload_batches_page(
    session: Session,
    *,
    context: TenantContext,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[UploadBatchSummary]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if type(limit) is not int or limit not in {50, 100}:
        raise storage_error("invalid_page_size")
    scope = {"kind": "upload-batches", "tenant": str(context.tenant_id)}
    query = select(UploadBatch).where(
        UploadBatch.tenant_id == context.tenant_id,
    )
    total = count_rows(session, query)
    if cursor:
        value, identity = decode_material_cursor(cursor, scope=scope)
        try:
            after = datetime.fromisoformat(value)
            if after.tzinfo is None:
                raise ValueError
        except ValueError:
            raise storage_error("invalid_cursor") from None
        query = query.where(
            or_(
                col(UploadBatch.created_at) < after,
                and_(
                    col(UploadBatch.created_at) == after, col(UploadBatch.id) < identity
                ),
            )
        )
    # Limit the parent page before aggregating children, never expand files into
    # Python or call the full per-batch progress API for directory entries.
    page = (
        query.order_by(col(UploadBatch.created_at).desc(), col(UploadBatch.id).desc())
        .limit(limit + 1)
        .subquery()
    )
    batch = aliased(UploadBatch, page)
    rows = session.exec(
        select(batch, func.count(col(ObjectUpload.id)))
        .outerjoin(
            ObjectUpload,
            and_(
                col(ObjectUpload.tenant_id) == batch.tenant_id,
                col(ObjectUpload.bc_id) == batch.bc_id,
                col(ObjectUpload.batch_id) == batch.id,
            ),
        )
        .group_by(*page.c)
        .order_by(col(batch.created_at).desc(), col(batch.id).desc())
        .execution_options(populate_existing=True)
    ).all()
    return Page(
        items=[
            UploadBatchSummary(
                batch_id=row.id,
                bc_id=row.bc_id,
                status=cast(UploadStage, row.status),
                file_count=count,
                created_at=row.created_at,
            )
            for row, count in rows[:limit]
        ],
        next_cursor=encode_material_cursor(
            scope=scope,
            name=rows[limit - 1][0].created_at.isoformat(),
            identity=rows[limit - 1][0].id,
        )
        if len(rows) > limit
        else None,
        total=total,
    )


def original_preview(
    session: Session, *, context: TenantContext, material_id: UUID
) -> SignedPreview:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    file = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.id == material_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if file is None:
        raise storage_error("material_not_found")
    require_bc(session, context=context, bc_id=file.bc_id, action="read")
    if file.storage_state != "stored":
        raise storage_error("upload_not_ready")
    original = file.current_object
    if original and original.storage_provider == "external":
        from .push_worker import external_source_url

        return SignedPreview(
            url=external_source_url(session, context=context, material_id=file.id)
        )
    if file.object_key != object_key_for(context.tenant_id, file.id):
        raise storage_error("object_identity_unverified")
    return SignedPreview(
        url=sign_original_preview(key=file.object_key, mime_type=file.mime_type)
    )


def account_video_preview(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    bc_id: str,
    video_id: str,
    advertiser_ids: tuple[str, ...],
) -> RemoteMaterialPreview:
    """广告报表素材详情的成片预览：按账户与平台视频 ID 读取，不要求素材在素材库里。"""
    from .remote_sources import read_account_video_preview

    preview = read_account_video_preview(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        bc_id=bc_id,
        video_id=video_id,
        advertiser_ids=advertiser_ids,
    )
    return RemoteMaterialPreview(
        bc_id=bc_id,
        url=preview.url,
        advertiser_id=preview.advertiser_id,
        video_id=preview.video_id,
        width=preview.width,
        height=preview.height,
        duration=preview.duration,
        format=preview.format,
    )
