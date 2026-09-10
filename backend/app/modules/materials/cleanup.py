"""Receipt-bound maintenance of immutable temporary objects; no account writes."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.tenants.models import AuditEvent

from .cleanup_abandoned import abandon_transport, abort_original, parts_removed
from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
    record_milestone,
)
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
)
from .object_budget import locked_object, release_object_reservation
from .storage import make_object_s3, storage_error

register_dispatch_task("materials.cleanup_original", "resources")
CLEANUP_HARD_LIMIT = 120
BACKOFF_SECONDS = (10, 30, 60, 120, 300)


def _file(session: Session, obj: TemporaryMaterialObject) -> IngestSessionFile:
    return session.exec(
        select(IngestSessionFile).where(
            IngestSessionFile.tenant_id == obj.tenant_id,
            IngestSessionFile.bc_id == obj.bc_id,
            IngestSessionFile.material_id == obj.material_id,
        )
    ).one()


def _receipt(
    session: Session, obj: TemporaryMaterialObject, source_receipt_id: UUID
) -> dict[str, Any]:
    operation = session.get(
        MaterialAssetOperation, source_receipt_id, populate_existing=True
    )
    file = session.get(MaterialFile, obj.material_id)
    if (
        not operation
        or not file
        or file.current_object_generation != obj.generation
        or (operation.tenant_id, operation.bc_id, operation.material_id)
        != (obj.tenant_id, obj.bc_id, obj.material_id)
        or operation.path != "upload_original"
        or operation.status != "succeeded"
        or operation.remote_response.get("object_id") != str(obj.id)
        or operation.remote_response.get("generation") != obj.generation
        or not obj.digest_verified_at
        or not obj.video_md5
        or not obj.sha256
        or obj.actual_bytes != obj.expected_bytes
        or (file.sha256, file.video_md5) != (obj.sha256, obj.video_md5)
    ):
        raise storage_error("cleanup_unverified")
    attempt = session.exec(
        select(MaterialUploadAttempt).where(
            MaterialUploadAttempt.tenant_id == obj.tenant_id,
            MaterialUploadAttempt.operation_id == source_receipt_id,
            MaterialUploadAttempt.status == "available",
        )
    ).one_or_none()
    asset = session.exec(
        select(AccountMaterial).where(
            AccountMaterial.tenant_id == obj.tenant_id,
            AccountMaterial.bc_id == obj.bc_id,
            AccountMaterial.material_id == obj.material_id,
            AccountMaterial.advertiser_id == operation.advertiser_id,
            AccountMaterial.status == "available",
        )
    ).one_or_none()
    if (
        not attempt
        or not asset
        or asset.connection_id != attempt.connection_id
        or asset.video_id != operation.remote_response.get("video_id")
        or asset.verified_at is None
        or asset.verified_at < obj.digest_verified_at
    ):
        raise storage_error("cleanup_unverified")
    return {
        "source_receipt_id": str(source_receipt_id),
        "asset_id": str(asset.id),
        "advertiser_id": asset.advertiser_id,
        "video_id": asset.video_id,
        "sha256": obj.sha256,
        "video_md5": obj.video_md5,
        "expected_bytes": obj.expected_bytes,
    }


def _queue(
    session: Session, obj: TemporaryMaterialObject, cleanup: ObjectCleanup
) -> UUID:
    row = _file(session, obj)
    parent = session.get(IngestSession, row.session_id)
    assert parent
    if cleanup.dispatch_id:
        existing = session.get(PendingDispatch, cleanup.dispatch_id)
        if existing and existing.payload == {
            "cleanup_id": str(cleanup.id),
            "generation": obj.generation,
        }:
            return existing.id
    cleanup.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=obj.tenant_id, actor_id=parent.actor_id, role="operator"
        ),
        task_name="materials.cleanup_original",
        task_key=f"original-cleanup:{cleanup.id}:{obj.generation}",
        payload={"cleanup_id": str(cleanup.id), "generation": obj.generation},
    )
    return cleanup.dispatch_id


def schedule_cleanup(
    session: Session, *, object_id: UUID, source_receipt_id: UUID
) -> UUID:
    obj = locked_object(session, object_id=object_id)
    evidence = _receipt(session, obj, source_receipt_id)
    cleanup = session.exec(
        select(ObjectCleanup).where(
            ObjectCleanup.tenant_id == obj.tenant_id,
            ObjectCleanup.bc_id == obj.bc_id,
            ObjectCleanup.material_id == obj.material_id,
            ObjectCleanup.generation == obj.generation,
        )
    ).one_or_none()
    if cleanup is None:
        cleanup = ObjectCleanup(
            tenant_id=obj.tenant_id,
            bc_id=obj.bc_id,
            material_id=obj.material_id,
            generation=obj.generation,
            reason="verified_source",
            eligibility_evidence=evidence,
        )
        session.add(cleanup)
        session.flush()
    elif cleanup.status == "deleted":
        return cleanup.id
    else:
        cleanup.reason, cleanup.eligibility_evidence = "verified_source", evidence
    if obj.status in {"verified", "stored"}:
        obj.status = "cleanup_pending"
    _queue(session, obj, cleanup)
    session.flush()
    return cleanup.id


def _active_uses(session: Session, obj: TemporaryMaterialObject) -> list[OriginalUse]:
    now = datetime.now(UTC)
    uses = session.exec(
        select(OriginalUse).where(
            OriginalUse.tenant_id == obj.tenant_id,
            OriginalUse.bc_id == obj.bc_id,
            OriginalUse.material_id == obj.material_id,
            OriginalUse.generation == obj.generation,
            OriginalUse.status == "active",
        )
    ).all()
    active = []
    for use in uses:
        # Local preview permission ends at its hard 5-minute authorization bound.
        # Validation has an enforced prefork process lifetime plus a guard margin.
        locally_ended = use.purpose == "preview" and use.expires_at <= now
        locally_ended |= (
            use.purpose == "validation"
            and use.expires_at + timedelta(seconds=30) <= now
            and (obj.claimed_until is None or obj.claimed_until <= now)
        )
        if locally_ended:
            use.status, use.released_at = "expired", now
            use.revision += 1
        else:
            active.append(use)
    session.flush()
    return active


def _head_absent(s3: Any, obj: TemporaryMaterialObject) -> bool:
    try:
        s3.head_object(Bucket=obj.storage_bucket, Key=obj.object_key)
        return False
    except ClientError as error:
        response = error.response
        if response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        ) == 404 and response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return True
        raise storage_error("object_storage_unavailable") from None


def _defer(
    session: Session, cleanup: ObjectCleanup, *, code: str, unknown: bool = False
) -> None:
    cleanup.error_code = code
    cleanup.status = "delete_unknown" if unknown else "pending"
    cleanup.claim_token, cleanup.claimed_until = None, None
    delay = BACKOFF_SECONDS[
        min(max(cleanup.attempt_count - 1, 0), len(BACKOFF_SECONDS) - 1)
    ]
    cleanup.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
    if cleanup.dispatch_id:
        dispatch = session.get(PendingDispatch, cleanup.dispatch_id)
        if dispatch:
            dispatch.available_at, dispatch.published_at = cleanup.next_attempt_at, None


def _finish_deleted(
    session: Session, obj: TemporaryMaterialObject, cleanup: ObjectCleanup
) -> None:
    now = datetime.now(UTC)
    cleanup.status, cleanup.head_confirmed_at = "deleted", now
    # A post-send HEAD404 is positive deletion evidence even after a lost reply.
    if cleanup.delete_sent_at:
        cleanup.delete_confirmed_at = cleanup.delete_confirmed_at or now
    cleanup.claim_token, cleanup.claimed_until, cleanup.error_code = None, None, None
    obj.status, obj.deleted_at, obj.error_code = "deleted", now, None
    session.flush()
    release_object_reservation(
        session, object_id=obj.id, deletion_evidence_id=cleanup.id
    )
    row = _file(session, obj)
    record_milestone(
        session,
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        session_id=row.session_id,
        material_id=obj.material_id,
        milestone="cleaned",
    )
    parent = session.get(IngestSession, row.session_id)
    assert parent
    session.add(
        AuditEvent(
            tenant_id=obj.tenant_id,
            actor_id=parent.actor_id,
            action="materials.cleanup_confirmed",
            target_id=str(obj.id),
            details={
                "maintenance": True,
                "generation": obj.generation,
                "cleanup_id": str(cleanup.id),
                "reason": cleanup.reason,
            },
        )
    )
    # Platform mapping and original filename/digests remain unchanged.
    file = session.get(MaterialFile, obj.material_id)
    if file and file.current_object_generation == obj.generation:
        file.storage_state = "unavailable"


def run_cleanup(
    *,
    database_engine: Any,
    cleanup_id: UUID,
    s3: Any = None,
    tenant_id: UUID | None = None,
    generation: int | None = None,
) -> None:
    now, owner = datetime.now(UTC), uuid4()
    with Session(database_engine) as session, session.begin():
        cleanup = session.get(ObjectCleanup, cleanup_id)
        if (
            not cleanup
            or (tenant_id and cleanup.tenant_id != tenant_id)
            or (generation is not None and cleanup.generation != generation)
        ):
            return
        identity = session.exec(
            select(TemporaryMaterialObject.id).where(
                TemporaryMaterialObject.tenant_id == cleanup.tenant_id,
                TemporaryMaterialObject.bc_id == cleanup.bc_id,
                TemporaryMaterialObject.material_id == cleanup.material_id,
                TemporaryMaterialObject.generation == cleanup.generation,
            )
        ).one_or_none()
        if identity is None:
            return
        obj = locked_object(session, object_id=identity)
        session.refresh(cleanup)
        if (
            cleanup.status == "deleted"
            or cleanup.next_attempt_at > now
            or (cleanup.claimed_until and cleanup.claimed_until > now)
        ):
            return
        readback = cleanup.delete_sent_at is not None or bool(
            cleanup.eligibility_evidence.get("abort_sent_at")
        )
        transport = cleanup.eligibility_evidence.get("transport", "delete")
        if not settings.MATERIAL_CLEANUP_ENABLED and not readback:
            _defer(session, cleanup, code="cleanup_disabled")
            return
        if (
            not obj.storage_provider
            or not obj.storage_bucket
            or obj.storage_endpoint is None
        ):
            _defer(
                session, cleanup, code="object_namespace_unverified", unknown=readback
            )
            return
        if not readback:
            try:
                if cleanup.reason == "verified_source":
                    evidence = _receipt(
                        session,
                        obj,
                        UUID(cleanup.eligibility_evidence["source_receipt_id"]),
                    )
                    if evidence != cleanup.eligibility_evidence:
                        raise storage_error("cleanup_unverified")
                else:
                    transport = abandon_transport(session, obj, cleanup)
                uses = _active_uses(session, obj)
                if any(
                    transport != "abort" or use.purpose != "part_put" for use in uses
                ):
                    raise storage_error("original_in_use")
            except (DomainError, KeyError, ValueError) as error:
                _defer(
                    session,
                    cleanup,
                    code=error.code
                    if isinstance(error, DomainError)
                    else "cleanup_unverified",
                    unknown=readback,
                )
                return
        cleanup.claim_token, cleanup.claimed_until = (
            owner,
            now + timedelta(seconds=CLEANUP_HARD_LIMIT + 30),
        )
        cleanup.attempt_count += 1
        cleanup.status = "delete_unknown" if readback else "deleting"
        obj.status = cleanup.status
        if not readback and transport == "abort":
            cleanup.eligibility_evidence = {
                **cleanup.eligibility_evidence,
                "transport": "abort",
                "abort_sent_at": now.isoformat(),
                "upload_id": obj.s3_upload_id,
            }
        elif not readback:
            cleanup.delete_sent_at = now
        # Persist a recovery delivery before leaving the transaction; a hard-killed
        # worker never strands a claim whose broker message was already delivered.
        if cleanup.dispatch_id:
            dispatch = session.get(PendingDispatch, cleanup.dispatch_id)
            if dispatch:
                dispatch.available_at, dispatch.published_at = (
                    cleanup.claimed_until,
                    None,
                )
        session.flush()
        session.expunge(obj)
    absent, aborted, failure = False, False, None
    try:
        client = s3 or make_object_s3(obj)
        if transport == "abort":
            if readback:
                aborted = parts_removed(client, obj)
            if not aborted and settings.MATERIAL_CLEANUP_ENABLED:
                abort_original(client, obj)
                aborted = parts_removed(client, obj)
            if aborted:
                absent = _head_absent(client, obj)
        elif readback:
            absent = _head_absent(client, obj)
            if not absent and settings.MATERIAL_CLEANUP_ENABLED:
                client.delete_object(Bucket=obj.storage_bucket, Key=obj.object_key)
                absent = _head_absent(client, obj)
        else:
            client.delete_object(Bucket=obj.storage_bucket, Key=obj.object_key)
            absent = _head_absent(client, obj)
        if not absent:
            failure = "cleanup_result_pending"
    except Exception:
        failure = "object_storage_unavailable"
    with Session(database_engine) as session, session.begin():
        obj = locked_object(session, object_id=identity)
        cleanup = session.get(ObjectCleanup, cleanup_id, populate_existing=True)
        if not cleanup or cleanup.claim_token != owner:
            return
        if aborted:
            cleanup.abort_confirmed_at = datetime.now(UTC)
        if absent and transport == "abort" and _active_uses(session, obj):
            # Abort and empty ListParts cannot prove an unknown in-flight PUT
            # has ended. Keep its reservation until its own completion evidence.
            obj.status = "delete_unknown"
            _defer(session, cleanup, code="part_result_unknown", unknown=True)
        elif absent:
            _finish_deleted(session, obj, cleanup)
        else:
            obj.status = "delete_unknown"
            _defer(
                session, cleanup, code=failure or "cleanup_result_pending", unknown=True
            )


def repair_cleanups(session: Session, *, limit: int = 100) -> int:
    now = datetime.now(UTC)
    identities = session.exec(
        select(ObjectCleanup.id)
        .where(
            col(ObjectCleanup.status).in_(("pending", "deleting", "delete_unknown")),
            col(ObjectCleanup.next_attempt_at) <= now,
        )
        .order_by(col(ObjectCleanup.next_attempt_at), col(ObjectCleanup.id))
        .limit(min(max(limit, 1), 100))
    ).all()
    count = 0
    for identity in identities:
        row = session.get(ObjectCleanup, identity)
        assert row
        object_id = session.exec(
            select(TemporaryMaterialObject.id).where(
                TemporaryMaterialObject.tenant_id == row.tenant_id,
                TemporaryMaterialObject.bc_id == row.bc_id,
                TemporaryMaterialObject.material_id == row.material_id,
                TemporaryMaterialObject.generation == row.generation,
            )
        ).one_or_none()
        if object_id is None:
            continue
        obj = locked_object(session, object_id=object_id)
        session.refresh(row)
        if row.claimed_until and row.claimed_until > now:
            continue
        identity = _queue(session, obj, row)
        dispatch = session.get(PendingDispatch, identity)
        assert dispatch
        if (
            dispatch.published_at
            and dispatch.published_at <= now - timedelta(seconds=120)
            and dispatch.available_at <= now
        ):
            dispatch.published_at = None
            count += 1
    return count
