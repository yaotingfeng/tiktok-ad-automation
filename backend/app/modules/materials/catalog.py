"""Bounded upload history and authorized read-only original capabilities."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page

from .models import MaterialFile, ObjectUpload, UploadBatch
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
    bc_id: str,
    material_id: UUID,
) -> RemoteMaterialPreview:
    """Read-only SDK INFO with socket budget; no DNS-wide process deadline claim.

    This dedicated HTTP control-plane request never changes readiness, queues
    work or issues an original-use permission. Sources and authority are fresh.
    """
    from .remote_sources import read_remote_source, resolve_remote_source
    from .source_uploads import READ_HARD_LIMIT

    deadline = datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
    with Session(database_engine) as db:
        require_bc(db, context=context, bc_id=bc_id, action="read")
        source = resolve_remote_source(
            db, context=context, bc_id=bc_id, material_id=material_id
        )
        if source is None:
            raise DomainError(
                "material_remote_source_unavailable", "暂无法预览，请恢复来源授权或补传"
            )
        source_id = source.id
    preview = read_remote_source(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        source_asset_id=source_id,
        deadline=deadline,
        hard_limit=READ_HARD_LIMIT,
    )
    return RemoteMaterialPreview(
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
    bc_id: str,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[UploadBatchSummary]:
    require_bc(session, context=context, bc_id=bc_id, action="read")
    if type(limit) is not int or limit not in {50, 100}:
        raise storage_error("invalid_page_size")
    scope = {"kind": "upload-batches", "tenant": str(context.tenant_id), "bc": bc_id}
    query = select(UploadBatch).where(
        UploadBatch.tenant_id == context.tenant_id,
        UploadBatch.bc_id == bc_id,
    )
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
    )


def original_preview(
    session: Session, *, context: TenantContext, bc_id: str, material_id: UUID
) -> SignedPreview:
    require_bc(session, context=context, bc_id=bc_id, action="read")
    file = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == bc_id,
            MaterialFile.id == material_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if file is None:
        raise storage_error("material_not_found")
    if file.storage_state != "stored":
        raise storage_error("upload_not_ready")
    if file.object_key != object_key_for(context.tenant_id, file.id):
        raise storage_error("object_identity_unverified")
    return SignedPreview(
        url=sign_original_preview(key=file.object_key, mime_type=file.mime_type)
    )
