"""Atomic temporary-byte accounting. Helpers flush; callers own the transaction.

Lock order: material, object, global budget, tenant budget, session counter.
Stored bytes are a subset of reserved bytes, never a second capacity charge.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.models import TenantBC
from app.modules.tenants.permissions import require_tenant

from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    ObjectBudget,
    ObjectCleanup,
    TemporaryMaterialObject,
)
from .models import MaterialFile


def locked_object(
    session: Session,
    *,
    object_id: UUID,
    context: TenantContext | None = None,
    current: bool = False,
    action: str = "upload",
) -> TemporaryMaterialObject:
    # Initial lookup is read-only; re-read after acquiring the material lock.
    row = session.get(TemporaryMaterialObject, object_id)
    if row is None or (context and row.tenant_id != context.tenant_id):
        raise DomainError("material_not_found", "素材不存在")
    if context:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action=action,
        )
        bc = session.get(
            TenantBC, (context.tenant_id, row.bc_id), populate_existing=True
        )
        if bc is None or bc.ownership_conflict:
            raise DomainError("account_not_in_bc", "素材 BC 不可用")
    file = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.id == row.material_id, MaterialFile.tenant_id == row.tenant_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    row = session.exec(
        select(TemporaryMaterialObject)
        .where(TemporaryMaterialObject.id == object_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if current and file.current_object_generation != row.generation:
        raise DomainError("object_generation_changed", "素材上传代次已更新")
    return row


def _budgets(session: Session, tenant_id: UUID) -> tuple[ObjectBudget, ObjectBudget]:
    result = []
    for key, tenant in (("global", None), (f"tenant:{tenant_id}", tenant_id)):
        session.exec(
            insert(ObjectBudget)
            .values(
                scope_key=key,
                tenant_id=tenant,
                reserved_bytes=0,
                stored_bytes=0,
                revision=0,
            )
            .on_conflict_do_nothing(index_elements=["scope_key"])
        )
        result.append(
            session.exec(
                select(ObjectBudget)
                .where(ObjectBudget.scope_key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
        )
    return result[0], result[1]


def _session_delta(
    session: Session,
    obj: TemporaryMaterialObject,
    *,
    reserved: int = 0,
    stored: int = 0,
) -> None:
    session_ids = select(IngestSessionFile.session_id).where(
        IngestSessionFile.tenant_id == obj.tenant_id,
        IngestSessionFile.bc_id == obj.bc_id,
        IngestSessionFile.material_id == obj.material_id,
    )
    session.exec(
        update(IngestSession)
        .where(
            col(IngestSession.tenant_id) == obj.tenant_id,
            col(IngestSession.id).in_(session_ids),
        )
        .values(
            reserved_bytes=IngestSession.reserved_bytes + reserved,
            stored_bytes=IngestSession.stored_bytes + stored,
            revision=IngestSession.revision + 1,
        )
        .execution_options(synchronize_session=False)
    )


def reserve_object(
    session: Session, *, context: TenantContext, object_id: UUID, byte_size: int
) -> bool:
    obj = locked_object(session, object_id=object_id, context=context, current=True)
    if type(byte_size) is not int or byte_size != obj.expected_bytes:
        raise DomainError("invalid_file", "预留字节必须与受理文件一致")
    if obj.reservation_released_at or obj.status in {
        "cleanup_pending",
        "deleting",
        "delete_unknown",
        "deleted",
        "missing",
    }:
        raise DomainError("object_unavailable", "原件已关闭接收")
    if obj.reserved_bytes:
        if obj.reserved_bytes != byte_size:
            raise DomainError("object_budget_corrupt", "暂存预留需要核查")
        return True
    global_budget, tenant_budget = _budgets(session, obj.tenant_id)
    if (
        global_budget.reserved_bytes + byte_size
        > settings.MATERIAL_STORAGE_GLOBAL_BYTES
        or tenant_budget.reserved_bytes + byte_size
        > settings.MATERIAL_STORAGE_TENANT_BYTES
    ):
        return False
    for budget in (global_budget, tenant_budget):
        budget.reserved_bytes += byte_size
        budget.revision += 1
    obj.reserved_bytes = byte_size
    obj.reserved_at = datetime.now(UTC)
    obj.status = "reserved"
    _session_delta(session, obj, reserved=byte_size)
    session.flush()
    return True


def mark_object_stored(
    session: Session, *, context: TenantContext, object_id: UUID, actual_bytes: int
) -> None:
    obj = locked_object(session, object_id=object_id, context=context, current=True)
    if (
        type(actual_bytes) is not int
        or actual_bytes != obj.expected_bytes
        or obj.reserved_bytes != actual_bytes
        or obj.reservation_released_at
    ):
        raise DomainError("incomplete_object", "原件大小或暂存预留不一致")
    if obj.status not in {"reserved", "receiving", "stored", "validating", "verified"}:
        raise DomainError("object_unavailable", "原件已关闭接收")
    if obj.received_at is not None:
        return
    for budget in _budgets(session, obj.tenant_id):
        budget.stored_bytes += actual_bytes
        budget.revision += 1
    obj.actual_bytes = actual_bytes
    obj.received_at = datetime.now(UTC)
    obj.status = "stored"
    _session_delta(session, obj, stored=actual_bytes)
    session.flush()


def release_object_reservation(
    session: Session, *, object_id: UUID, deletion_evidence_id: UUID
) -> None:
    obj = locked_object(session, object_id=object_id)
    evidence = session.get(ObjectCleanup, deletion_evidence_id, populate_existing=True)
    if (
        not evidence
        or (
            evidence.tenant_id,
            evidence.bc_id,
            evidence.material_id,
            evidence.generation,
        )
        != (obj.tenant_id, obj.bc_id, obj.material_id, obj.generation)
        or evidence.status != "deleted"
        or evidence.head_confirmed_at is None
        or obj.status != "deleted"
        or obj.deleted_at is None
    ):
        raise DomainError("cleanup_unverified", "临时文件尚未确认删除")
    if evidence.delete_confirmed_at is None and evidence.abort_confirmed_at is None:
        raise DomainError("cleanup_unverified", "缺少删除或分片关闭证据")
    if obj.reservation_released_at:
        return
    size = obj.reserved_bytes
    stored = size if obj.received_at is not None else 0
    for budget in _budgets(session, obj.tenant_id):
        if budget.reserved_bytes < size or budget.stored_bytes < stored:
            raise DomainError("object_budget_corrupt", "暂存预留需要核查")
        budget.reserved_bytes -= size
        budget.stored_bytes -= stored
        budget.revision += 1
    obj.reserved_bytes = 0
    obj.reservation_released_at = datetime.now(UTC)
    _session_delta(session, obj, reserved=-size, stored=-stored)
    session.flush()
