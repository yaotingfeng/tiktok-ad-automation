"""Durable signing windows and explicit browser transport receipts.

Receipts never release space. Unknown exchanges remain unknown even if another
attempt later uploads the same part; Complete or safe cleanup must settle them.
"""

from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

from sqlmodel import Session, col, select

from app.core.context import TenantContext

from .ingest_models import OriginalUse, TemporaryMaterialObject
from .ingest_schemas import (
    IngestIdentity,
    IngestPartPermission,
    IngestPartPermissions,
    IngestPartReceipt,
    IngestPartReceiptsCreate,
    IngestPartReceiptsResult,
)
from .object_uses import acquire_original_use
from .storage import storage_error


def verify_signed_deadline(url: str, deadline: datetime) -> None:
    from datetime import timedelta

    try:
        fields = parse_qs(urlsplit(url).query, strict_parsing=True)
        dates, durations = fields["X-Amz-Date"], fields["X-Amz-Expires"]
        if len(dates) != 1 or len(durations) != 1:
            raise ValueError
        issued = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        duration = int(durations[0])
        if not 1 <= duration <= 900 or issued + timedelta(seconds=duration) > deadline:
            raise ValueError
    except KeyError, TypeError, ValueError, OverflowError:
        # Never return a capability whose actual wire deadline is later than
        # its durable permission, even after signer/credential setup stalls.
        raise storage_error("part_permission_expired") from None


def signing_window(
    db: Session,
    *,
    context: TenantContext,
    obj: TemporaryMaterialObject,
    request_id: UUID,
    numbers: list[int],
    ttl: int,
) -> list[OriginalUse]:
    existing = window_uses(db, obj, request_id)
    if existing:
        if sorted(use.completion_evidence["part_number"] for use in existing) != sorted(
            numbers
        ):
            raise storage_error("idempotency_conflict")
        if any(
            use.status != "active" or use.completion_evidence.get("outcome") != "signed"
            for use in existing
        ):
            raise storage_error("part_permission_closed")
        return existing
    uses = []
    for number in numbers:
        use = acquire_original_use(
            db,
            context=context,
            object_id=obj.id,
            purpose="part_put",
            operation_id=uuid5(obj.id, f"part:{request_id}:{number}"),
            lifetime_seconds=ttl,
        )
        use.completion_evidence = {
            "request_id": str(request_id),
            "part_number": number,
            "outcome": "signed",
            "upload_id": obj.s3_upload_id,
        }
        uses.append(use)
    db.flush()
    return uses


def window_uses(
    db: Session, obj: TemporaryMaterialObject, request_id: UUID
) -> list[OriginalUse]:
    return list(
        db.exec(
            select(OriginalUse)
            .where(
                OriginalUse.tenant_id == obj.tenant_id,
                OriginalUse.bc_id == obj.bc_id,
                OriginalUse.material_id == obj.material_id,
                OriginalUse.generation == obj.generation,
                OriginalUse.purpose == "part_put",
                col(OriginalUse.completion_evidence)["request_id"].as_string()
                == str(request_id),
            )
            .order_by(col(OriginalUse.id))
            .limit(3)
        ).all()
    )


def read_permissions(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    request_id: UUID,
) -> IngestPartPermissions:
    from .ingest_transport import _locked

    with Session(database_engine) as db:
        _, obj, _ = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            check_revision=False,
        )
        uses = window_uses(db, obj, request_id)
        if len(uses) > 2:
            raise storage_error("object_identity_unverified")
        assert obj.s3_upload_id
        return IngestPartPermissions(
            generation=obj.generation,
            upload_id=obj.s3_upload_id,
            operation_revision=obj.revision,
            request_id=request_id,
            items=[
                IngestPartPermission(
                    part_number=use.completion_evidence["part_number"],
                    permission_id=use.id,
                    permission_nonce=use.nonce,
                    permission_revision=use.revision,
                    outcome=use.completion_evidence.get("outcome", "signed"),
                )
                for use in uses
            ],
        )


def _use(
    db: Session, obj: TemporaryMaterialObject, receipt: IngestPartReceipt
) -> OriginalUse:
    use = db.get(OriginalUse, receipt.permission_id, populate_existing=True)
    if not use or (
        use.tenant_id,
        use.bc_id,
        use.material_id,
        use.generation,
        use.purpose,
        use.nonce,
        use.completion_evidence.get("part_number"),
        use.completion_evidence.get("upload_id"),
    ) != (
        obj.tenant_id,
        obj.bc_id,
        obj.material_id,
        obj.generation,
        "part_put",
        receipt.permission_nonce,
        receipt.part_number,
        obj.s3_upload_id,
    ):
        raise storage_error("version_conflict")
    # Completion may have closed the capability while its ACK response was lost.
    if use.status == "active" and use.revision != receipt.permission_revision:
        raise storage_error("version_conflict")
    previous = use.completion_evidence.get("outcome", "signed")
    if previous not in {"signed", receipt.outcome}:
        raise storage_error("part_result_unknown")
    if previous == "completed" and use.completion_evidence.get("etag") != receipt.etag:
        raise storage_error("version_conflict")
    return use


def acknowledge_parts(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestPartReceiptsCreate,
    s3: Any = None,
) -> IngestPartReceiptsResult:
    from . import storage
    from .ingest_transport import _close, _locked, _part_bytes, _snapshot

    if len({receipt.permission_id for receipt in identity.receipts}) != len(
        identity.receipts
    ):
        raise storage_error("invalid_part")
    pending = []
    with Session(database_engine) as db, db.begin():
        _, obj, _ = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            check_revision=False,
        )
        for receipt in identity.receipts:
            use = _use(db, obj, receipt)
            if (
                use.status == "active"
                and receipt.outcome == "completed"
                and use.completion_evidence.get("outcome") == "signed"
            ):
                pending.append(receipt)
        snapshot = _snapshot(obj)
    client = s3
    try:
        if pending:
            client = client if client is not None else storage.make_object_s3(snapshot)
        for receipt in pending:
            reply = client.list_parts(
                Bucket=snapshot.storage_bucket,
                Key=snapshot.object_key,
                UploadId=snapshot.s3_upload_id,
                MaxParts=1,
                PartNumberMarker=receipt.part_number - 1,
            )
            parts = reply.get("Parts") if isinstance(reply, dict) else None
            if (
                not isinstance(parts, list)
                or len(parts) != 1
                or (
                    parts[0].get("PartNumber"),
                    parts[0].get("Size"),
                    str(parts[0].get("ETag", "")).strip('"'),
                )
                != (
                    receipt.part_number,
                    _part_bytes(snapshot, receipt.part_number),
                    cast(str, receipt.etag).strip('"'),
                )
            ):
                raise storage_error("part_receipt_unverified")
    finally:
        _close(client, owned=s3 is None)
    with Session(database_engine) as db, db.begin():
        _, obj, _ = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            check_revision=False,
        )
        for receipt in identity.receipts:
            use = _use(db, obj, receipt)
            use.completion_evidence = {
                **use.completion_evidence,
                "outcome": receipt.outcome,
                "etag": receipt.etag,
                "acknowledged_at": use.completion_evidence.get("acknowledged_at")
                or datetime.now(UTC).isoformat(),
            }
        assert obj.s3_upload_id
        return IngestPartReceiptsResult(
            generation=obj.generation,
            upload_id=obj.s3_upload_id,
            operation_revision=obj.revision,
            accepted_permission_ids=[
                receipt.permission_id for receipt in identity.receipts
            ],
        )
