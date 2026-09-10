"""Bounded metadata registration and constant-cost ingest summaries.

No storage/provider I/O. Caller owns commit. Chunks insert <=200 identities;
only their final atomic counter delta touches the shared session header.
"""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.materials.ingest_models import (
    IngestChunk,
    IngestMilestone,
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
    canonical_object_key,
)
from app.modules.materials.ingest_schemas import (
    IngestChunkCreate,
    IngestChunkResult,
    IngestFilePublic,
    IngestSealIssue,
    IngestSealResult,
    IngestSessionCreate,
    IngestSummary,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.repository import (
    decode_material_cursor,
    encode_material_cursor,
)
from app.modules.materials.storage import part_layout
from app.modules.materials.uploads import public_error, require_bc


def require_ingest_storage(*, reconciliation: bool = False) -> None:
    if not reconciliation and not getattr(settings, "MATERIAL_INGEST_ENABLED", False):
        raise DomainError("ingest_disabled", "批量导入尚未启用")
    if settings.OBJECT_STORAGE_PROVIDER != "r2":
        raise DomainError("object_storage_unconfigured", "批量导入需要私有 R2 存储")
    settings.require_object_storage()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def get_session(
    db: Session,
    *,
    context: TenantContext,
    session_id: UUID,
    action: str = "read",
    lock: bool = False,
) -> IngestSession:
    statement = (
        select(IngestSession)
        .where(
            col(IngestSession.tenant_id) == context.tenant_id,
            col(IngestSession.id) == session_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    row = db.exec(statement).one_or_none()
    if row is None:
        raise DomainError("upload_batch_not_found", "导入会话不存在或不可见")
    require_bc(db, context=context, bc_id=row.bc_id, action=action)
    return row


def summary(row: IngestSession) -> IngestSummary:
    return IngestSummary(
        session_id=row.id,
        bc_id=row.bc_id,
        expected_count=row.expected_files,
        total_bytes=row.expected_bytes,
        status=row.status,
        registration_cursor=row.registration_cursor,
        accepted_count=row.accepted_count,
        uploaded_count=row.uploaded_count,
        ready_count=row.ready_count,
        failed_count=row.failed_count,
        cleaned_count=row.cleaned_count,
        reserved_bytes=row.reserved_bytes,
        stored_bytes=row.stored_bytes,
        created_at=row.created_at,
    )


def create_session(
    db: Session, *, context: TenantContext, body: IngestSessionCreate
) -> IngestSummary:
    require_bc(db, context=context, bc_id=body.bc_id, action="upload")
    request_digest = digest(body.model_dump(mode="json", exclude={"request_id"}))
    existing = db.exec(
        select(IngestSession).where(
            col(IngestSession.tenant_id) == context.tenant_id,
            col(IngestSession.request_id) == body.request_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "请求标识已绑定另一份导入清单")
        return summary(existing)
    require_ingest_storage()
    row = IngestSession(
        tenant_id=context.tenant_id,
        bc_id=body.bc_id,
        actor_id=context.actor_id,
        request_id=body.request_id,
        request_digest=request_digest,
        expected_files=body.file_count,
        expected_bytes=body.total_bytes,
    )
    inserted = db.exec(
        insert(IngestSession)
        .values(row.model_dump())
        .on_conflict_do_nothing(constraint="uq_ingest_session_request")
        .returning(col(IngestSession.id))
    ).first()
    if inserted is None:
        existing = db.exec(
            select(IngestSession).where(
                col(IngestSession.tenant_id) == context.tenant_id,
                col(IngestSession.request_id) == body.request_id,
            )
        ).one()
        if existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "请求标识已绑定另一份导入清单")
        return summary(existing)
    return summary(row)


def file_statement(context: TenantContext, session_id: UUID) -> Any:
    return (
        select(
            IngestSessionFile,
            MaterialFile,
            TemporaryMaterialObject,
        )
        .join(
            MaterialFile,
            and_(
                col(MaterialFile.tenant_id) == col(IngestSessionFile.tenant_id),
                col(MaterialFile.bc_id) == col(IngestSessionFile.bc_id),
                col(MaterialFile.id) == col(IngestSessionFile.material_id),
            ),
        )
        .outerjoin(
            TemporaryMaterialObject,
            and_(
                col(TemporaryMaterialObject.tenant_id)
                == col(IngestSessionFile.tenant_id),
                col(TemporaryMaterialObject.bc_id) == col(IngestSessionFile.bc_id),
                col(TemporaryMaterialObject.material_id)
                == col(IngestSessionFile.material_id),
                col(TemporaryMaterialObject.generation)
                == col(IngestSessionFile.current_generation),
            ),
        )
        .where(
            col(IngestSessionFile.tenant_id) == context.tenant_id,
            col(IngestSessionFile.session_id) == session_id,
        )
        .execution_options(populate_existing=True)
    )


def file_public(
    row: IngestSessionFile,
    material: MaterialFile,
    original: TemporaryMaterialObject | None,
) -> IngestFilePublic:
    part_size, part_count = part_layout(row.byte_size)
    stage = {
        "registered": "receiving",
        "waiting_capacity": "receiving",
        "validating": "verifying",
        "failed": "blocked",
        "cancelled": "blocked",
    }.get(row.status, row.status)
    internal_status = original.error_code if original and original.error_code else ""
    operation_status = {
        "multipart_creating": "initializing",
        "multipart_create_unknown": "result_unknown",
        "multipart_collecting": "completing",
        "multipart_completing": "completing",
        "multipart_complete_unknown": "result_unknown",
    }.get(internal_status, "idle")
    return IngestFilePublic(
        material_id=material.id,
        client_index=row.client_index,
        file_name=material.file_name,
        size=row.byte_size,
        mime_type=material.mime_type,
        last_modified_ms=row.last_modified_ms,
        generation=row.current_generation,
        upload_id=original.s3_upload_id if original else None,
        part_size=original.part_size if original else part_size,
        part_count=(row.byte_size + original.part_size - 1) // original.part_size
        if original
        else part_count,
        operation_revision=original.revision if original else row.revision,
        platform_status=stage,
        temporary_storage_status=original.status if original else "missing",
        received_bytes=row.byte_size if original and original.received_at else 0,
        source_advertiser_id=row.source_advertiser_id,
        can_retry=(
            row.status in {"failed", "blocked", "cancelled"}
            and original is not None
            and (
                original.status == "deleted"
                and original.reservation_released_at is not None
                or original.status == "waiting_capacity"
                and original.reserved_at is None
                and original.s3_upload_id is None
                and original.received_at is None
            )
            and internal_status
            not in {"multipart_create_unknown", "multipart_complete_unknown"}
        ),
        error_code=(
            "user_cancelled"
            if row.error_code == "user_cancelled"
            else public_error(
                row.error_code
                or (
                    internal_status
                    if internal_status and operation_status == "idle"
                    else None
                )
            )
        ),
        operation_status=operation_status,
        task_id=row.dispatch_id,
    )


def read_file(
    db: Session, *, context: TenantContext, session_id: UUID, material_id: UUID
) -> IngestFilePublic:
    get_session(db, context=context, session_id=session_id)
    result = db.exec(
        file_statement(context, session_id).where(
            col(IngestSessionFile.material_id) == material_id
        )
    ).one_or_none()
    if result is None:
        raise DomainError("material_not_found", "素材不存在或不可见")
    return file_public(*result)


def chunk_result(
    db: Session, *, context: TenantContext, receipt: IngestChunk
) -> IngestChunkResult:
    rows = db.exec(
        file_statement(context, receipt.session_id)
        .where(col(IngestSessionFile.client_index).in_(receipt.client_indexes))
        .order_by(col(IngestSessionFile.client_index))
    ).all()
    return IngestChunkResult(
        session_id=receipt.session_id,
        request_id=receipt.request_id,
        items=[file_public(*row) for row in rows],
    )


def register_chunk(
    db: Session, *, context: TenantContext, session_id: UUID, body: IngestChunkCreate
) -> IngestChunkResult:
    parent = get_session(db, context=context, session_id=session_id, action="upload")
    ordered = sorted(body.files, key=lambda item: item.client_index)
    request_digest = digest([item.model_dump(mode="json") for item in ordered])
    existing = db.exec(
        select(IngestChunk).where(
            col(IngestChunk.tenant_id) == context.tenant_id,
            col(IngestChunk.session_id) == session_id,
            col(IngestChunk.request_id) == body.request_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "子批次请求内容不一致")
        return chunk_result(db, context=context, receipt=existing)
    require_ingest_storage()
    if parent.status != "registering":
        raise DomainError("version_conflict", "导入清单已冻结")
    maximum = getattr(settings, "MATERIAL_URL_MAX_UPLOAD_BYTES", 256 * 1024**2)
    if any(
        item.client_index >= parent.expected_files or item.size > maximum
        for item in ordered
    ):
        raise DomainError("invalid_file", "文件序号或大小超过本次导入上限")
    receipt = IngestChunk(
        tenant_id=context.tenant_id,
        bc_id=parent.bc_id,
        session_id=session_id,
        request_id=body.request_id,
        request_digest=request_digest,
        client_indexes=[item.client_index for item in ordered],
    )
    inserted = db.exec(
        insert(IngestChunk)
        .values(receipt.model_dump())
        .on_conflict_do_nothing(constraint="uq_ingest_chunk_request")
        .returning(col(IngestChunk.id))
    ).first()
    if inserted is None:
        existing = db.exec(
            select(IngestChunk).where(
                col(IngestChunk.tenant_id) == context.tenant_id,
                col(IngestChunk.session_id) == session_id,
                col(IngestChunk.request_id) == body.request_id,
            )
        ).one()
        if existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "子批次请求内容不一致")
        return chunk_result(db, context=context, receipt=existing)
    known = {
        row.client_index: row
        for row in db.exec(
            select(IngestSessionFile).where(
                col(IngestSessionFile.tenant_id) == context.tenant_id,
                col(IngestSessionFile.session_id) == session_id,
                col(IngestSessionFile.client_index).in_(receipt.client_indexes),
            )
        ).all()
    }
    materials, candidates = [], []
    for item in ordered:
        manifest_digest = digest(item.model_dump(mode="json"))
        if item.client_index in known:
            if known[item.client_index].manifest_digest != manifest_digest:
                raise DomainError(
                    "idempotency_conflict", "相同文件序号的清单内容已改变"
                )
            continue
        identity = uuid4()
        materials.append(
            MaterialFile(
                id=identity,
                tenant_id=context.tenant_id,
                bc_id=parent.bc_id,
                file_name=item.file_name,
                file_name_folded=item.file_name.casefold(),
                object_key=canonical_object_key(
                    context.tenant_id, parent.bc_id, identity, 1
                ),
                byte_size=item.size,
                mime_type=item.mime_type,
                current_object_generation=1,
            ).model_dump()
        )
        candidates.append(
            IngestSessionFile(
                tenant_id=context.tenant_id,
                bc_id=parent.bc_id,
                session_id=session_id,
                client_index=item.client_index,
                material_id=identity,
                byte_size=item.size,
                manifest_digest=manifest_digest,
                last_modified_ms=item.last_modified_ms,
                status="waiting_capacity",
            ).model_dump()
        )
    new_ids: set[UUID] = set()
    if materials:
        db.exec(insert(MaterialFile).values(materials))
        new_ids = set(
            db.exec(
                insert(IngestSessionFile)
                .values(candidates)
                .on_conflict_do_nothing(constraint="uq_ingest_client_index")
                .returning(col(IngestSessionFile.material_id))
            )
            .scalars()
            .all()
        )
        unused = [value["id"] for value in materials if value["id"] not in new_ids]
        if unused:
            db.exec(delete(MaterialFile).where(col(MaterialFile.id).in_(unused)))
        actual = db.exec(
            select(IngestSessionFile).where(
                col(IngestSessionFile.tenant_id) == context.tenant_id,
                col(IngestSessionFile.session_id) == session_id,
                col(IngestSessionFile.client_index).in_(receipt.client_indexes),
            )
        ).all()
        wanted = {
            item.client_index: digest(item.model_dump(mode="json")) for item in ordered
        }
        if any(row.manifest_digest != wanted[row.client_index] for row in actual):
            raise DomainError("idempotency_conflict", "相同文件序号的清单内容已改变")
    fresh = [value for value in materials if value["id"] in new_ids]
    if fresh:
        originals = [
            TemporaryMaterialObject(
                tenant_id=context.tenant_id,
                bc_id=parent.bc_id,
                material_id=value["id"],
                generation=1,
                object_key=value["object_key"],
                expected_bytes=value["byte_size"],
                part_size=part_layout(value["byte_size"])[0],
                storage_provider=settings.OBJECT_STORAGE_PROVIDER,
                storage_endpoint=settings.S3_ENDPOINT_URL,
                storage_bucket=settings.S3_BUCKET,
            ).model_dump()
            for value in fresh
        ]
        db.exec(insert(TemporaryMaterialObject).values(originals))
        db.exec(
            insert(IngestMilestone).values(
                [
                    IngestMilestone(
                        tenant_id=context.tenant_id,
                        bc_id=parent.bc_id,
                        session_id=session_id,
                        material_id=value["id"],
                        milestone="accepted",
                        byte_size=value["byte_size"],
                    ).model_dump()
                    for value in fresh
                ]
            )
        )
    accepted_bytes = sum(value["byte_size"] for value in fresh)
    advanced = db.exec(
        update(IngestSession)
        .where(
            col(IngestSession.id) == session_id,
            col(IngestSession.tenant_id) == context.tenant_id,
            col(IngestSession.status) == "registering",
            col(IngestSession.accepted_count) + len(fresh)
            <= col(IngestSession.expected_files),
            col(IngestSession.accepted_bytes) + accepted_bytes
            <= col(IngestSession.expected_bytes),
        )
        .values(
            accepted_count=col(IngestSession.accepted_count) + len(fresh),
            accepted_bytes=col(IngestSession.accepted_bytes) + accepted_bytes,
            registration_cursor=col(IngestSession.accepted_count) + len(fresh),
            revision=col(IngestSession.revision) + 1,
        )
        .returning(col(IngestSession.id))
    ).first()
    if advanced is None:
        raise DomainError("idempotency_conflict", "清单已冻结或文件总量超出声明")
    return chunk_result(db, context=context, receipt=receipt)


def seal_session(
    db: Session, *, context: TenantContext, session_id: UUID
) -> IngestSealResult:
    row = get_session(
        db, context=context, session_id=session_id, action="upload", lock=True
    )
    issues = []
    if row.accepted_count != row.expected_files:
        issues.append(
            IngestSealIssue(
                code="file_count_mismatch",
                expected=row.expected_files,
                accepted=row.accepted_count,
            )
        )
    if row.accepted_bytes != row.expected_bytes:
        issues.append(
            IngestSealIssue(
                code="total_bytes_mismatch",
                expected=row.expected_bytes,
                accepted=row.accepted_bytes,
            )
        )
    if not issues and row.status == "registering":
        row.status = "sealed"
        row.revision += 1
        db.flush()
    return IngestSealResult(
        sealed=row.status == "sealed", issues=issues, summary=summary(row)
    )


def files_page(
    db: Session,
    *,
    context: TenantContext,
    session_id: UUID,
    cursor: str | None = None,
    limit: int = 100,
    status: str | None = None,
) -> Page[IngestFilePublic]:
    parent = get_session(db, context=context, session_id=session_id)
    if not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "页大小必须在1到100之间")
    scope = {
        "tenant": str(context.tenant_id),
        "bc": parent.bc_id,
        "session": str(session_id),
        "status": status or "",
        "kind": "ingest_files",
    }
    statement = file_statement(context, session_id)
    if status:
        statement = statement.where(col(IngestSessionFile.status) == status)
    if cursor:
        marker, identity = decode_material_cursor(cursor, scope=scope)
        try:
            index = int(marker)
            if index < 0 or index >= parent.expected_files:
                raise ValueError
        except ValueError:
            raise DomainError("invalid_cursor", "导入游标无效") from None
        statement = statement.where(
            or_(
                col(IngestSessionFile.client_index) > index,
                and_(
                    col(IngestSessionFile.client_index) == index,
                    col(IngestSessionFile.material_id) > identity,
                ),
            )
        )
    rows = db.exec(
        statement.order_by(
            col(IngestSessionFile.client_index), col(IngestSessionFile.material_id)
        ).limit(limit + 1)
    ).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        next_cursor = encode_material_cursor(
            scope=scope, name=str(last.client_index), identity=last.material_id
        )
    return Page(
        items=[file_public(*row) for row in rows[:limit]], next_cursor=next_cursor
    )


def sessions_page(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[IngestSummary]:
    require_bc(db, context=context, bc_id=bc_id, action="read")
    scope = {"tenant": str(context.tenant_id), "bc": bc_id, "kind": "ingest_sessions"}
    statement = select(IngestSession).where(
        col(IngestSession.tenant_id) == context.tenant_id,
        col(IngestSession.bc_id) == bc_id,
    )
    if cursor:
        marker, identity = decode_material_cursor(cursor, scope=scope)
        try:
            moment = datetime.fromisoformat(marker)
            if moment.tzinfo is None:
                raise ValueError
        except ValueError:
            raise DomainError("invalid_cursor", "导入游标无效") from None
        statement = statement.where(
            or_(
                col(IngestSession.created_at) < moment,
                and_(
                    col(IngestSession.created_at) == moment,
                    col(IngestSession.id) < identity,
                ),
            )
        )
    rows = db.exec(
        statement.order_by(
            col(IngestSession.created_at).desc(), col(IngestSession.id).desc()
        ).limit(limit + 1)
    ).all()
    next_cursor = (
        encode_material_cursor(
            scope=scope,
            name=rows[limit - 1].created_at.isoformat(),
            identity=rows[limit - 1].id,
        )
        if len(rows) > limit
        else None
    )
    return Page(items=[summary(row) for row in rows[:limit]], next_cursor=next_cursor)
