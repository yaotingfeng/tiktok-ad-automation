"""Durable permission to use one immutable object generation.

Expiration is descriptive for remote reads/PUTs: it is never completion proof.
All issuance and cleanup eligibility serialize on the same material/object lock.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError

from .ingest_models import ObjectCleanup, OriginalUse
from .models import AccountMaterial
from .object_budget import locked_object


def acquire_original_use(
    session: Session,
    *,
    context: TenantContext,
    object_id: UUID,
    purpose: str,
    operation_id: UUID | None = None,
    dispatch_id: UUID | None = None,
    lifetime_seconds: int = 300,
) -> OriginalUse:
    allowed = {
        "part_put": ({"receiving"}, 900),
        "validation": ({"stored", "validating"}, 660),
        "ingest": ({"verified"}, 7200),
        "preview": ({"verified"}, 300),
    }
    if (
        purpose not in allowed
        or type(lifetime_seconds) is not int
        or not 0 < lifetime_seconds <= allowed[purpose][1]
        or (purpose == "ingest" and operation_id is None)
    ):
        raise DomainError("object_use_invalid", "原件使用请求无效")
    obj = locked_object(
        session,
        object_id=object_id,
        context=context,
        current=True,
        action="read" if purpose == "preview" else "upload",
    )
    if (
        obj.status not in allowed[purpose][0]
        or obj.reservation_released_at
        or (purpose == "part_put" and obj.reserved_bytes != obj.expected_bytes)
    ):
        raise DomainError("object_unavailable", "原件当前不可读取或接收")
    cleanup = session.exec(
        select(ObjectCleanup.id)
        .where(
            ObjectCleanup.tenant_id == obj.tenant_id,
            ObjectCleanup.bc_id == obj.bc_id,
            ObjectCleanup.material_id == obj.material_id,
            ObjectCleanup.generation == obj.generation,
        )
        .limit(1)
    ).first()
    if cleanup:
        raise DomainError("object_cleanup_pending", "原件已进入清理阶段")
    if (
        purpose == "preview"
        and session.exec(
            select(AccountMaterial.id)
            .where(
                AccountMaterial.tenant_id == obj.tenant_id,
                AccountMaterial.bc_id == obj.bc_id,
                AccountMaterial.material_id == obj.material_id,
                AccountMaterial.status == "available",
            )
            .limit(1)
        ).first()
    ):
        raise DomainError("remote_preview_required", "请从广告账户读取预览")
    if operation_id is not None:
        existing = session.exec(
            select(OriginalUse)
            .where(
                OriginalUse.tenant_id == obj.tenant_id,
                OriginalUse.bc_id == obj.bc_id,
                OriginalUse.material_id == obj.material_id,
                OriginalUse.generation == obj.generation,
                OriginalUse.purpose == purpose,
                OriginalUse.operation_id == operation_id,
                OriginalUse.status == "active",
            )
            .order_by(col(OriginalUse.created_at))
            .limit(1)
        ).first()
        if existing:
            if purpose == "part_put":
                now = datetime.now(UTC)
                existing.permission_issued_at = now
                existing.expires_at = max(
                    existing.expires_at, now + timedelta(seconds=lifetime_seconds)
                )
                existing.revision += 1
                session.flush()
            return existing
    now = datetime.now(UTC)
    use = OriginalUse(
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        material_id=obj.material_id,
        generation=obj.generation,
        actor_id=context.actor_id,
        purpose=purpose,
        operation_id=operation_id,
        dispatch_id=dispatch_id,
        permission_issued_at=now,
        expires_at=now + timedelta(seconds=lifetime_seconds),
    )
    session.add(use)
    session.flush()
    return use


def release_original_use(session: Session, *, use_id: UUID, nonce: UUID) -> bool:
    use = session.get(OriginalUse, use_id)
    if use is None or use.nonce != nonce:
        return False
    from .ingest_models import TemporaryMaterialObject

    object_id = session.exec(
        select(TemporaryMaterialObject.id).where(
            TemporaryMaterialObject.tenant_id == use.tenant_id,
            TemporaryMaterialObject.bc_id == use.bc_id,
            TemporaryMaterialObject.material_id == use.material_id,
            TemporaryMaterialObject.generation == use.generation,
        )
    ).one()
    locked_object(session, object_id=object_id)
    session.refresh(use)
    if use.nonce != nonce or use.status != "active":
        return False
    use.status = "released"
    use.released_at = datetime.now(UTC)
    use.revision += 1
    session.flush()
    return True


def release_object_uses(
    session: Session, *, object_id: UUID, purpose: str, operation_id: UUID | None = None
) -> int:
    """Internal completion hook; caller must possess exact completion evidence."""
    if purpose not in {"part_put", "validation", "ingest", "preview"} or (
        purpose == "ingest" and operation_id is None
    ):
        raise DomainError("object_use_invalid", "原件使用结束证据范围无效")
    obj = locked_object(session, object_id=object_id)
    query = select(OriginalUse).where(
        OriginalUse.tenant_id == obj.tenant_id,
        OriginalUse.bc_id == obj.bc_id,
        OriginalUse.material_id == obj.material_id,
        OriginalUse.generation == obj.generation,
        OriginalUse.purpose == purpose,
        OriginalUse.status == "active",
    )
    if operation_id is not None:
        query = query.where(OriginalUse.operation_id == operation_id)
    uses = session.exec(query).all()
    now = datetime.now(UTC)
    for use in uses:
        use.status, use.released_at = "released", now
        use.revision += 1
    session.flush()
    return len(uses)
