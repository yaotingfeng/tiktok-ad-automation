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
        # 先取原上传 BC 的合法来源，再取其他真实副本；授权与实际 BC 在 SQL 中关联。
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
                AccountMaterial.video_id != "",
                grant,
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
    if file.object_key != object_key_for(context.tenant_id, file.id):
        raise storage_error("object_identity_unverified")
    return SignedPreview(
        url=sign_original_preview(key=file.object_key, mime_type=file.mime_type)
    )
