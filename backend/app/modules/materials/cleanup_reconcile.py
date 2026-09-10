"""Bounded abandonment selection and read-only tenant/BC orphan inspection.

No object delete/abort or reservation release lives here. Eligible objects get
an intent consumed by cleanup_original, which must recheck physical evidence.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, tuple_
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.models import TenantBC
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
    canonical_object_key,
    transition_ingest_file,
)
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
)
from .repository import decode_material_cursor, encode_material_cursor
from .uploads import require_bc

register_dispatch_task("materials.cleanup_original", "resources")
SCAN_STATUSES = (
    "reserved",
    "receiving",
    "stored",
    "validating",
    "verified",
    "cleanup_pending",
)
TRANSPORT_UNKNOWN = frozenset(
    {
        "multipart_creating",
        "multipart_create_unknown",
        "multipart_completing",
        "multipart_complete_unknown",
    }
)


def _window(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "核查窗口必须在1到100之间")


def _scope(context: TenantContext | None, bc_id: str | None) -> dict[str, str]:
    return {
        "kind": "abandonment",
        "tenant": str(context.tenant_id) if context else "*",
        "bc": bc_id or "*",
    }


def _authorize(
    db: Session, context: TenantContext, bc_id: str | None, *, enqueue: bool
) -> None:
    action = "upload" if enqueue else "read"
    require_tenant(
        db, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )
    if bc_id:
        require_bc(db, context=context, bc_id=bc_id, action=action)


def _object_predicates(model: Any, obj: TemporaryMaterialObject) -> tuple[Any, ...]:
    return (
        model.tenant_id == obj.tenant_id,
        model.bc_id == obj.bc_id,
        model.material_id == obj.material_id,
    )


def _evaluate(
    db: Session, *, object_id: UUID, now: datetime, enqueue: bool
) -> dict[str, Any]:
    candidate = db.get(TemporaryMaterialObject, object_id)
    if candidate is None:
        return {
            "object_id": str(object_id),
            "reason": "missing_ledger",
            "queued": False,
        }
    result: dict[str, Any] = {
        "object_id": str(object_id),
        "generation": candidate.generation,
        "queued": False,
    }
    file = db.exec(
        select(MaterialFile)
        .where(
            col(MaterialFile.id) == candidate.material_id,
            col(MaterialFile.tenant_id) == candidate.tenant_id,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if file is None:
        return {**result, "reason": "owner_busy"}
    obj = db.exec(
        select(TemporaryMaterialObject)
        .where(col(TemporaryMaterialObject.id) == object_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if obj is None:
        return {**result, "reason": "owner_busy"}
    row = db.exec(
        select(IngestSessionFile)
        .where(*_object_predicates(IngestSessionFile, obj))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    bc = db.get(TenantBC, (obj.tenant_id, obj.bc_id))
    if (
        row is None
        or bc is None
        or bc.ownership_conflict
        or file.bc_id != obj.bc_id
        or file.current_object_generation != obj.generation
        or row.current_generation != obj.generation
    ):
        return {**result, "reason": "scope_or_generation_unverified"}
    if (
        obj.storage_provider != "r2"
        or not obj.storage_endpoint
        or not obj.storage_bucket
        or obj.object_key
        != canonical_object_key(
            obj.tenant_id, obj.bc_id, obj.material_id, obj.generation
        )
    ):
        return {**result, "reason": "object_namespace_unverified"}
    existing = db.exec(
        select(ObjectCleanup).where(
            *_object_predicates(ObjectCleanup, obj),
            col(ObjectCleanup.generation) == obj.generation,
        )
    ).one_or_none()
    if existing:
        return {**result, "reason": "intent_exists"}
    if (
        not obj.reserved_at
        or obj.reserved_bytes <= 0
        or obj.reservation_released_at
        or obj.status not in SCAN_STATUSES
    ):
        return {**result, "reason": "not_reserved"}
    if obj.claimed_until is not None and obj.claimed_until > now:
        return {**result, "reason": "owner_busy"}
    last_use = db.exec(
        select(
            func.max(
                func.coalesce(
                    col(OriginalUse.permission_issued_at), col(OriginalUse.created_at)
                )
            )
        ).where(
            *_object_predicates(OriginalUse, obj),
            col(OriginalUse.generation) == obj.generation,
        )
    ).one()
    last_attempt = db.exec(
        select(func.max(col(MaterialUploadAttempt.created_at))).where(
            *_object_predicates(MaterialUploadAttempt, obj)
        )
    ).one()
    progress = max(
        value
        for value in (
            obj.reserved_at,
            obj.received_at,
            obj.digest_verified_at,
            last_use,
            last_attempt,
        )
        if value is not None
    )
    result["last_progress_at"] = progress.isoformat()
    if progress > now - timedelta(seconds=settings.MATERIAL_ABANDON_SECONDS):
        return {**result, "reason": "recent_progress"}

    def blocked(reason: str) -> dict[str, Any]:
        if enqueue:
            details = {
                "object_id": str(obj.id),
                "generation": obj.generation,
                "reason": reason,
                "last_progress_at": progress.isoformat(),
            }
            # All writers hold the same object lock. Repeated maintenance passes
            # must not grow an unchanged unknown-result alert without bound.
            existing_alert = db.exec(
                select(col(AuditEvent.id))
                .where(
                    col(AuditEvent.tenant_id) == obj.tenant_id,
                    col(AuditEvent.action) == "materials.abandonment_blocked",
                    col(AuditEvent.target_id) == str(obj.id),
                    col(AuditEvent.details).contains(details),
                )
                .limit(1)
            ).first()
            if existing_alert is None:
                parent = db.get(IngestSession, row.session_id)
                assert parent is not None
                db.add(
                    AuditEvent(
                        tenant_id=obj.tenant_id,
                        actor_id=parent.actor_id,
                        action="materials.abandonment_blocked",
                        target_id=str(obj.id),
                        details=details,
                    )
                )
        return {**result, "reason": reason}

    if obj.error_code in TRANSPORT_UNKNOWN:
        return blocked("transport_result_unknown")
    operations = db.exec(
        select(MaterialAssetOperation)
        .where(
            *_object_predicates(MaterialAssetOperation, obj),
            col(MaterialAssetOperation.status).in_(
                [
                    "pending",
                    "sending",
                    "result_unknown",
                    "verifying",
                    "confirmed_absent",
                    "succeeded",
                ]
            ),
        )
        .limit(101)
        .with_for_update()
    ).all()
    if len(operations) > 100:
        return blocked("operation_window_exceeded")
    if any(
        op.status in {"sending", "result_unknown", "verifying", "confirmed_absent"}
        or (
            op.status == "pending"
            and (op.attempt_token is not None or bool(op.remote_response))
        )
        for op in operations
    ):
        return blocked("source_result_unknown")
    source = db.exec(
        select(col(AccountMaterial.id))
        .where(
            *_object_predicates(AccountMaterial, obj),
            col(AccountMaterial.status) == "available",
        )
        .limit(1)
    ).first()
    if source is not None or any(op.status == "succeeded" for op in operations):
        return blocked("verified_source_receipt_required")
    unsafe_use = db.exec(
        select(col(OriginalUse.id))
        .where(
            *_object_predicates(OriginalUse, obj),
            col(OriginalUse.generation) == obj.generation,
            col(OriginalUse.status) == "active",
            ~(
                (col(OriginalUse.purpose) == "part_put")
                & (col(OriginalUse.expires_at) <= now)
            ),
        )
        .limit(1)
    ).first()
    if unsafe_use is not None:
        return blocked("original_use_active")
    if not enqueue:
        return {**result, "reason": "eligible"}
    # A pending operation with no attempt or remote evidence has not been sent.
    # Closing its intent under the material/object locks prevents later intake.
    for operation in operations:
        if operation.status == "pending":
            operation.status = "failed"
    cleanup = ObjectCleanup(
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        material_id=obj.material_id,
        generation=obj.generation,
        reason="abandoned",
        eligibility_evidence={
            "object_id": str(obj.id),
            "evaluated_at": now.isoformat(),
            "last_progress_at": progress.isoformat(),
            "physical_deletion_verified": False,
        },
    )
    db.add(cleanup)
    db.flush()
    obj.status = "cleanup_pending"
    obj.revision += 1
    transition_ingest_file(
        db,
        tenant_id=obj.tenant_id,
        file_id=row.id,
        expected_revision=row.revision,
        status="cancelled",
        error_code="abandoned",
    )
    parent = db.get(IngestSession, row.session_id)
    assert parent is not None
    cleanup.dispatch_id = enqueue_after_commit(
        db,
        context=TenantContext(
            tenant_id=obj.tenant_id, actor_id=parent.actor_id, role="operator"
        ),
        task_name="materials.cleanup_original",
        task_key=f"original-cleanup:{cleanup.id}:{obj.generation}",
        payload={"cleanup_id": str(cleanup.id), "generation": obj.generation},
    )
    return {
        **result,
        "reason": "abandoned",
        "queued": True,
        "cleanup_id": str(cleanup.id),
    }


def scan_abandoned_objects(
    db: Session,
    *,
    context: TenantContext | None = None,
    bc_id: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    enqueue: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Caller commits; persist next_cursor across Beat invocations, reset on empty.

    The existing (status,next_attempt_at,id) index supplies a bounded candidate
    window. Eligibility is rechecked under locks, including progress timestamps;
    next_attempt_at orders the scan and is never changed to fake last activity.
    """
    _window(limit)
    now = now or datetime.now(UTC)
    if context:
        _authorize(db, context, bc_id, enqueue=enqueue)
    elif bc_id:
        raise DomainError("material_request_invalid", "BC核查需要租户范围")
    if enqueue and not settings.MATERIAL_CLEANUP_ENABLED:
        return {
            "examined": 0,
            "queued": 0,
            "items": [],
            "next_cursor": cursor,
            "disabled": True,
        }
    scope = _scope(context, bc_id)
    statement = select(TemporaryMaterialObject).where(
        col(TemporaryMaterialObject.status).in_(SCAN_STATUSES),
        col(TemporaryMaterialObject.reserved_bytes) > 0,
        ~select(col(ObjectCleanup.id))
        .where(
            col(ObjectCleanup.tenant_id) == col(TemporaryMaterialObject.tenant_id),
            col(ObjectCleanup.bc_id) == col(TemporaryMaterialObject.bc_id),
            col(ObjectCleanup.material_id) == col(TemporaryMaterialObject.material_id),
            col(ObjectCleanup.generation) == col(TemporaryMaterialObject.generation),
        )
        .exists(),
    )
    if context:
        statement = statement.where(
            col(TemporaryMaterialObject.tenant_id) == context.tenant_id
        )
    if bc_id:
        statement = statement.where(col(TemporaryMaterialObject.bc_id) == bc_id)
    if cursor:
        name, identity = decode_material_cursor(cursor, scope=scope)
        try:
            status, moment = json.loads(name)
            moment = datetime.fromisoformat(moment)
            if status not in SCAN_STATUSES or moment.tzinfo is None:
                raise ValueError
        except ValueError, TypeError:
            raise DomainError("invalid_cursor", "闲置核查游标无效") from None
        statement = statement.where(
            tuple_(
                col(TemporaryMaterialObject.status),
                col(TemporaryMaterialObject.next_attempt_at),
                col(TemporaryMaterialObject.id),
            )
            > (status, moment, identity)
        )
    candidates = db.exec(
        statement.order_by(
            col(TemporaryMaterialObject.status),
            col(TemporaryMaterialObject.next_attempt_at),
            col(TemporaryMaterialObject.id),
        ).limit(limit)
    ).all()
    # Snapshot cursor before eligibility can mutate a candidate's status.
    following = (
        encode_material_cursor(
            scope=scope,
            name=json.dumps(
                [candidates[-1].status, candidates[-1].next_attempt_at.isoformat()]
            ),
            identity=candidates[-1].id,
        )
        if candidates
        else None
    )
    items = [
        _evaluate(db, object_id=obj.id, now=now, enqueue=enqueue) for obj in candidates
    ]
    return {
        "examined": len(items),
        "queued": sum(item["queued"] for item in items),
        "items": items,
        "next_cursor": following,
        "disabled": False,
    }


def scan_r2_orphans(
    *,
    database_engine: Any,
    context: TenantContext,
    bc_id: str,
    s3: Any,
    storage_provider: str,
    storage_endpoint: str,
    storage_bucket: str,
    kind: str = "objects",
    cursor: str | None = None,
    limit: int = 100,
    enqueue: bool = False,
) -> dict[str, Any]:
    """One scoped objects/multipart page outside DB transactions; unknowns report only."""
    _window(limit)
    if kind not in {"objects", "multipart"}:
        raise DomainError("material_request_invalid", "对象核查类型无效")
    with Session(database_engine) as db:
        _authorize(db, context, bc_id, enqueue=enqueue)
    prefix = f"tenants/{context.tenant_id}/bc/{quote(bc_id, safe='')}/materials/"
    namespace = hashlib.sha256(
        json.dumps([storage_provider, storage_endpoint, storage_bucket]).encode()
    ).hexdigest()
    scope = {
        "kind": f"r2-orphans:{kind}",
        "tenant": str(context.tenant_id),
        "bc": bc_id,
        "namespace": namespace,
    }
    marker = None
    if cursor:
        marker, _ = decode_material_cursor(cursor, scope=scope)
    markers = None
    if marker and kind == "multipart":
        try:
            markers = json.loads(marker)
            if (
                not isinstance(markers, list)
                or len(markers) != 2
                or any(not isinstance(value, str) for value in markers)
                or not markers[0].startswith(prefix)
            ):
                raise ValueError
        except ValueError, TypeError:
            raise DomainError("invalid_cursor", "分片核查游标无效") from None
    try:
        if kind == "objects":
            page = s3.list_objects_v2(
                Bucket=storage_bucket,
                Prefix=prefix,
                MaxKeys=limit,
                **({"ContinuationToken": marker} if marker else {}),
            )
        else:
            page = s3.list_multipart_uploads(
                Bucket=storage_bucket,
                Prefix=prefix,
                MaxUploads=limit,
                **(
                    {"KeyMarker": markers[0], "UploadIdMarker": markers[1]}
                    if markers
                    else {}
                ),
            )
    except BotoCoreError, ClientError:
        raise DomainError("object_storage_unavailable", "对象核查暂不可用") from None
    contents = (
        page.get("Contents" if kind == "objects" else "Uploads", [])
        if isinstance(page, dict)
        else None
    )
    if (
        not isinstance(contents, list)
        or len(contents) > limit
        or type(page.get("IsTruncated")) is not bool
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("Key"), str)
            or not item["Key"].startswith(prefix)
            or len(item["Key"]) > 1024
            for item in contents
        )
    ):
        raise DomainError("object_identity_unverified", "对象核查返回范围不一致")
    next_token = None
    if kind == "multipart" and any(
        not isinstance(value.get("UploadId"), str)
        or not 1 <= len(value["UploadId"]) <= 512
        for value in contents
    ):
        raise DomainError("object_identity_unverified", "分片核查返回身份无效")
    if page["IsTruncated"]:
        if kind == "objects":
            next_token = page.get("NextContinuationToken")
        else:
            key_marker, upload_marker = (
                page.get("NextKeyMarker"),
                page.get("NextUploadIdMarker"),
            )
            if (
                not isinstance(key_marker, str)
                or not key_marker.startswith(prefix)
                or not isinstance(upload_marker, str)
                or not contents
                or (key_marker, upload_marker)
                != (contents[-1]["Key"], contents[-1]["UploadId"])
            ):
                raise DomainError("invalid_cursor", "分片核查未向后推进")
            next_token = json.dumps([key_marker, upload_marker])
    if page["IsTruncated"] and (
        not isinstance(next_token, str)
        or not 1 <= len(next_token) <= 1000
        or next_token == marker
    ):
        raise DomainError("invalid_cursor", "对象核查未向后推进")
    items = []
    with Session(database_engine) as db, db.begin():
        _authorize(db, context, bc_id, enqueue=enqueue)
        keys = [item["Key"] for item in contents]
        known = (
            {
                obj.object_key: obj
                for obj in db.exec(
                    select(TemporaryMaterialObject).where(
                        col(TemporaryMaterialObject.tenant_id) == context.tenant_id,
                        col(TemporaryMaterialObject.bc_id) == bc_id,
                        col(TemporaryMaterialObject.object_key).in_(keys),
                    )
                ).all()
            }
            if keys
            else {}
        )
        for value in contents:
            key = value["Key"]
            item: dict[str, Any] = {
                "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                "classification": "unknown_owner",
            }
            obj = known.get(key)
            if obj:
                item.update(object_id=str(obj.id), generation=obj.generation)
                if (obj.storage_provider, obj.storage_endpoint, obj.storage_bucket) != (
                    storage_provider,
                    storage_endpoint,
                    storage_bucket,
                ):
                    item["classification"] = "namespace_mismatch"
                elif kind == "multipart" and obj.s3_upload_id != value["UploadId"]:
                    item["classification"] = "multipart_identity_mismatch"
                else:
                    item["classification"] = "bound"
                    if enqueue:
                        if not settings.MATERIAL_CLEANUP_ENABLED:
                            item["eligibility"] = "cleanup_disabled"
                        else:
                            item["eligibility"] = _evaluate(
                                db,
                                object_id=obj.id,
                                now=datetime.now(UTC),
                                enqueue=True,
                            )
            items.append(item)
    following = (
        encode_material_cursor(scope=scope, name=next_token, identity=UUID(int=0))
        if next_token
        else None
    )
    return {
        "items": items,
        "next_cursor": following,
        "examined": len(items),
        "read_only_storage": True,
    }
