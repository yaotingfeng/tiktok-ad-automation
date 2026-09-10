"""Bounded original verification and exact transactional stage handoff."""

import hashlib
import json
import math
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, or_, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
    record_milestone,
    transition_ingest_file,
)
from .models import MaterialAssetOperation, MaterialFile
from .object_budget import locked_object
from .object_uses import acquire_original_use, release_original_use
from .storage import make_object_s3, storage_error

register_dispatch_task("materials.validate_original", "resources")
register_dispatch_task("materials.upload_original", "resources")
VALIDATION_HARD_LIMIT = 660


def _ingest_file(session: Session, obj: TemporaryMaterialObject) -> IngestSessionFile:
    return session.exec(
        select(IngestSessionFile)
        .where(
            IngestSessionFile.tenant_id == obj.tenant_id,
            IngestSessionFile.bc_id == obj.bc_id,
            IngestSessionFile.material_id == obj.material_id,
        )
        .execution_options(populate_existing=True)
    ).one()


def enqueue_validation(
    session: Session, *, context: TenantContext, object_id: UUID
) -> UUID:
    obj = locked_object(session, object_id=object_id, context=context, current=True)
    row = _ingest_file(session, obj)
    if obj.status not in {"stored", "validating", "verified"}:
        raise storage_error("incomplete_object")
    if row.dispatch_id:
        existing = session.get(PendingDispatch, row.dispatch_id)
        if (
            existing
            and existing.payload.get("object_id") == str(obj.id)
            and existing.payload.get("generation") == obj.generation
        ):
            return existing.id
    if obj.status != "stored":
        raise storage_error("object_result_unknown")
    batch = session.get(IngestSession, row.session_id)
    assert batch
    # The immutable initiator remains the task's actor even when another operator
    # completes a transport request; its current permission is checked by worker.
    actor = TenantContext(
        tenant_id=obj.tenant_id, actor_id=batch.actor_id, role="operator"
    )
    obj.revision += 1
    # This new stage is runnable immediately. Its preceding multipart RPC's
    # claim deadline is not a validation retry delay. The idempotent return
    # above deliberately preserves backoff of an already-created validation.
    obj.next_attempt_at = datetime.now(UTC)
    payload = {
        "object_id": str(obj.id),
        "generation": obj.generation,
        "revision": obj.revision,
    }
    identity = enqueue_after_commit(
        session,
        context=actor,
        task_name="materials.validate_original",
        task_key=f"original-validation:{obj.id}:{obj.revision}",
        payload=payload,
    )
    row.dispatch_id = identity
    record_milestone(
        session,
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        session_id=row.session_id,
        material_id=obj.material_id,
        milestone="uploaded",
    )
    transition_ingest_file(
        session,
        tenant_id=obj.tenant_id,
        file_id=row.id,
        expected_revision=row.revision,
        status="stored",
    )
    session.flush()
    return identity


def _metadata(response: Any, obj: TemporaryMaterialObject, file: MaterialFile) -> None:
    if (
        not isinstance(response, dict)
        or type(response.get("ContentLength")) is not int
        or response["ContentLength"] != obj.expected_bytes
    ):
        raise storage_error("incomplete_object")
    metadata = response.get("Metadata")
    expected = {
        "tenant-id": str(obj.tenant_id),
        "bc-id": obj.bc_id,
        "material-id": str(obj.material_id),
        "generation": str(obj.generation),
    }
    if (
        not isinstance(metadata, dict)
        or any(metadata.get(key) != value for key, value in expected.items())
        or response.get("ContentType", "").split(";", 1)[0].lower()
        != file.mime_type.lower()
    ):
        raise storage_error("object_identity_unverified")


def inspect_video(path: Path, *, remaining: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "stream=codec_type,width,height:format=duration,format_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=max(0.1, remaining),
            check=False,
        )
        if result.returncode or len(result.stdout) > 64 * 1024:
            raise ValueError
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(item for item in streams if item.get("codec_type") == "video")
        width, height = video.get("width"), video.get("height")
        duration = float(data["format"]["duration"])
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError
        return {"width": width, "height": height, "duration": duration}
    except FileNotFoundError:
        raise storage_error("material_validator_unavailable") from None
    except ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError:
        raise storage_error("invalid_video") from None


def _read_original(
    s3: Any, obj: TemporaryMaterialObject, file: MaterialFile
) -> tuple[str, str, dict[str, Any]]:
    if not 0 < obj.expected_bytes <= settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
        raise storage_error("invalid_file")
    deadline = monotonic() + settings.MATERIAL_VALIDATION_SECONDS
    params = {"Bucket": obj.storage_bucket, "Key": obj.object_key}
    before = s3.head_object(**params)
    _metadata(before, obj, file)
    response = s3.get_object(**params)
    body = response.get("Body") if isinstance(response, dict) else None
    try:
        _metadata(response, obj, file)
        if body is None or not callable(getattr(body, "read", None)):
            raise storage_error("incomplete_object")
        sha256, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        length = 0
        with TemporaryDirectory(prefix="material-validation-") as temporary:
            path = Path(temporary) / "original"
            with path.open("wb") as target:
                while True:
                    if monotonic() >= deadline:
                        raise storage_error("material_deadline")
                    chunk = body.read(
                        min(8 * 1024 * 1024, obj.expected_bytes - length + 1)
                    )
                    if not chunk:
                        break
                    if (
                        not isinstance(chunk, bytes)
                        or length + len(chunk) > obj.expected_bytes
                    ):
                        raise storage_error("incomplete_object")
                    length += len(chunk)
                    sha256.update(chunk)
                    md5.update(chunk)
                    target.write(chunk)
            if length != obj.expected_bytes:
                raise storage_error("incomplete_object")
            media = inspect_video(path, remaining=deadline - monotonic())
        after = s3.head_object(**params)
        _metadata(after, obj, file)
        if before.get("ETag") and (
            before["ETag"] != response.get("ETag")
            or before["ETag"] != after.get("ETag")
        ):
            raise storage_error("object_identity_unverified")
        if monotonic() >= deadline:
            raise storage_error("material_deadline")
        return sha256.hexdigest(), md5.hexdigest(), media
    finally:
        if body is not None and callable(getattr(body, "close", None)):
            body.close()


def validate_original(
    *,
    database_engine: Any,
    context: TenantContext,
    object_id: UUID,
    s3: Any = None,
    dispatch_id: UUID | None = None,
    generation: int | None = None,
    revision: int | None = None,
) -> None:
    now = datetime.now(UTC)
    owner = uuid4()
    with Session(database_engine) as session, session.begin():
        obj = locked_object(session, object_id=object_id, context=context, current=True)
        row = _ingest_file(session, obj)
        dispatch = (
            session.get(PendingDispatch, row.dispatch_id) if row.dispatch_id else None
        )
        if (
            not dispatch
            or dispatch_id != dispatch.id
            or dispatch.actor_id != context.actor_id
            or dispatch.task_name != "materials.validate_original"
            or dispatch.payload
            != {
                "object_id": str(obj.id),
                "generation": generation,
                "revision": revision,
            }
            or (obj.generation, obj.revision) != (generation, revision)
            or obj.status not in {"stored", "validating"}
        ):
            return
        if obj.next_attempt_at > now or (obj.claimed_until and obj.claimed_until > now):
            return
        obj.status = "validating"
        obj.claim_token, obj.claimed_until = (
            owner,
            now + timedelta(seconds=VALIDATION_HARD_LIMIT + 30),
        )
        use = acquire_original_use(
            session,
            context=context,
            object_id=obj.id,
            purpose="validation",
            operation_id=owner,
            dispatch_id=dispatch.id,
            lifetime_seconds=VALIDATION_HARD_LIMIT,
        )
        use_id, use_nonce = use.id, use.nonce
        file = session.get(MaterialFile, obj.material_id)
        assert file
        session.expunge(obj)
        session.expunge(file)
    try:
        hashes = _read_original(s3 or make_object_s3(obj), obj, file)
        error_code = None
    except Exception as error:
        hashes = None
        error_code = (
            error.code
            if isinstance(error, DomainError)
            else "object_storage_unavailable"
        )
    with Session(database_engine) as session, session.begin():
        current = locked_object(session, object_id=object_id)
        # This exact local read has ended even if a successor owns the object.
        release_original_use(session, use_id=use_id, nonce=use_nonce)
        if (
            current.claim_token != owner
            or current.generation != generation
            or current.revision != revision
        ):
            return
        try:
            locked_object(session, object_id=object_id, context=context, current=True)
        except DomainError as error:
            hashes, error_code = None, error.code
        row = _ingest_file(session, current)
        current.claim_token, current.claimed_until = None, None
        if hashes is None:
            current.status, current.error_code = "stored", error_code
            current.next_attempt_at = datetime.now(UTC) + timedelta(seconds=60)
            transition_ingest_file(
                session,
                tenant_id=current.tenant_id,
                file_id=row.id,
                expected_revision=row.revision,
                status="failed",
                error_code=error_code,
            )
            dispatch = session.get(PendingDispatch, row.dispatch_id)
            if dispatch:
                dispatch.available_at, dispatch.published_at = (
                    current.next_attempt_at,
                    None,
                )
            return
        sha256, md5, media = hashes
        current.sha256, current.video_md5 = sha256, md5
        current.digest_verified_at = datetime.now(UTC)
        current.digest_source, current.status, current.error_code = (
            "worker_stream",
            "verified",
            None,
        )
        current.actual_bytes = current.expected_bytes
        file = session.get(MaterialFile, current.material_id)
        assert file
        file.sha256, file.video_md5 = sha256, md5
        file.digest_verified_at, file.digest_source = (
            current.digest_verified_at,
            current.digest_source,
        )
        file.width, file.height, file.duration = (
            media["width"],
            media["height"],
            media["duration"],
        )
        file.storage_state = "stored"
        payload = {
            "material_id": str(file.id),
            "object_id": str(current.id),
            "generation": current.generation,
        }
        row.dispatch_id = enqueue_after_commit(
            session,
            context=context,
            task_name="materials.upload_original",
            task_key=f"source-ingest:{current.id}:{current.generation}",
            payload=payload,
        )
        transition_ingest_file(
            session,
            tenant_id=current.tenant_id,
            file_id=row.id,
            expected_revision=row.revision,
            status="stored",
        )


def repair_validations(session: Session, *, limit: int = 100) -> int:
    now = datetime.now(UTC)
    ids = session.exec(
        select(TemporaryMaterialObject.id)
        .join(
            IngestSessionFile,
            (col(IngestSessionFile.tenant_id) == col(TemporaryMaterialObject.tenant_id))
            & (
                col(IngestSessionFile.material_id)
                == col(TemporaryMaterialObject.material_id)
            )
            & (
                col(IngestSessionFile.current_generation)
                == col(TemporaryMaterialObject.generation)
            ),
        )
        .join(
            PendingDispatch,
            col(PendingDispatch.id) == col(IngestSessionFile.dispatch_id),
        )
        .where(
            col(IngestSessionFile.status) != "available",
            col(PendingDispatch.published_at) <= now - timedelta(seconds=120),
            col(PendingDispatch.available_at) <= now,
            or_(
                col(TemporaryMaterialObject.claimed_until).is_(None),
                col(TemporaryMaterialObject.claimed_until) <= now,
            ),
            col(TemporaryMaterialObject.status).in_(
                ("stored", "validating", "verified")
            ),
            col(TemporaryMaterialObject.next_attempt_at) <= now,
        )
        .order_by(
            col(TemporaryMaterialObject.next_attempt_at),
            col(TemporaryMaterialObject.id),
        )
        .limit(min(max(limit, 1), 100))
    ).all()
    repaired = 0
    for identity in ids:
        obj = locked_object(session, object_id=identity)
        if obj.claimed_until and obj.claimed_until > now:
            continue
        row = session.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.tenant_id == obj.tenant_id,
                IngestSessionFile.material_id == obj.material_id,
                IngestSessionFile.current_generation == obj.generation,
            )
        ).first()
        if not row or row.status == "available" or not row.dispatch_id:
            continue
        dispatch = session.get(PendingDispatch, row.dispatch_id)
        if (
            not dispatch
            or dispatch.payload.get("object_id") != str(obj.id)
            or dispatch.payload.get("generation") != obj.generation
            or dispatch.available_at > now
        ):
            continue
        if dispatch.published_at and dispatch.published_at <= now - timedelta(
            seconds=120
        ):
            dispatch.published_at = None
            repaired += 1
    return repaired


def issue_ingest_url(
    session: Session,
    *,
    context: TenantContext,
    object_id: UUID,
    operation_id: UUID,
    s3: Any = None,
) -> str:
    obj = locked_object(session, object_id=object_id, context=context, current=True)
    operation = session.get(MaterialAssetOperation, operation_id)
    if (
        not operation
        or (operation.tenant_id, operation.bc_id, operation.material_id)
        != (obj.tenant_id, obj.bc_id, obj.material_id)
        or operation.status != "sending"
        or operation.path != "upload_original"
        or operation.attempt_token is None
    ):
        raise storage_error("object_use_invalid")
    if (
        not obj.storage_bucket
        or not obj.storage_provider
        or obj.storage_endpoint is None
    ):
        raise storage_error("object_namespace_unverified")
    acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="ingest",
        operation_id=operation_id,
        lifetime_seconds=settings.MATERIAL_INGEST_URL_SECONDS,
    )
    try:
        return str(
            (s3 or make_object_s3(obj)).generate_presigned_url(
                "get_object",
                Params={"Bucket": obj.storage_bucket, "Key": obj.object_key},
                ExpiresIn=settings.MATERIAL_INGEST_URL_SECONDS,
                HttpMethod="GET",
            )
        )
    except Exception:
        raise storage_error("object_storage_unavailable") from None


def sign_ingest_url(
    *,
    database_engine: Any,
    context: TenantContext,
    object_id: UUID,
    operation_id: UUID,
    s3: Any = None,
) -> str:
    with Session(database_engine) as session, session.begin():
        return issue_ingest_url(
            session,
            context=context,
            object_id=object_id,
            operation_id=operation_id,
            s3=s3,
        )
