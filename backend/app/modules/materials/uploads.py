"""Durable multipart phases. Every remote call runs outside a DB transaction.

start_upload_batch and finish_upload only flush. The explicitly named network
orchestrators own short transactions on independent sessions. No Celery publish
occurs here; only the transactional outbox is written.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import (
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
    ObjectUpload,
    UploadBatch,
)
from app.modules.materials.schemas import (
    SignedPart,
    UploadBatchResult,
    UploadedPart,
    UploadFileRequest,
    UploadFileResult,
    UploadStage,
)
from app.modules.materials.storage import (
    find_multipart,
    head_verified,
    make_s3,
    object_key_for,
    part_layout,
    sign_part,
    storage_error,
)
from app.modules.tenants.permissions import require_tenant

CLAIM_DURATION = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


def require_bc(
    session: Session, *, context: TenantContext, bc_id: str, action: str
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if bc is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    if action != "read" and bc.ownership_conflict:
        raise DomainError("account_ownership_conflict", "当前 BC 归属冲突")


def _upload(
    session: Session,
    context: TenantContext,
    material_id: UUID,
    *,
    lock: bool = False,
    action: str = "upload",
) -> ObjectUpload:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )
    statement = (
        select(ObjectUpload)
        .where(
            ObjectUpload.tenant_id == context.tenant_id,
            ObjectUpload.material_id == material_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    row = session.exec(statement).one_or_none()
    if row is None:
        raise storage_error("material_not_found")
    require_bc(session, context=context, bc_id=row.bc_id, action=action)
    return row


SAFE_ERROR_CODES = frozenset(
    {
        "no_upload_account",
        "sdk_upload_capacity_exceeded",
        "upload_connection_changed",
        "material_deadline",
        "material_digest_missing",
        "unsupported_material_schema",
        "material_result_pending",
        "material_reconciliation_ambiguous",
        "material_response_unknown",
        "admission_policy_invalid",
        "admission_unconfigured",
        "admission_unavailable",
        "admission_deferred",
        "tiktok_response_error",
        "connection_unavailable",
        "credential_invalid",
        "account_access_denied",
        "account_ownership_conflict",
        "account_metadata_incomplete",
        "account_not_in_bc",
        "action_forbidden",
        "tenant_forbidden",
        "invalid_file",
        "invalid_part",
        "incomplete_object",
        "object_identity_unverified",
        "object_result_unknown",
        "object_storage_unavailable",
    }
)


def public_error(code: object) -> str | None:
    if code is None:
        return None
    return (
        code
        if isinstance(code, str) and code in SAFE_ERROR_CODES
        else "material_response_unknown"
    )


def _result(
    row: ObjectUpload,
    file: MaterialFile,
    attempt: MaterialUploadAttempt | None = None,
    operation: MaterialAssetOperation | None = None,
) -> UploadFileResult:
    stage: UploadStage = "receiving"
    error_code = public_error(row.error_code)
    if row.status == "stored":
        stage = "stored"
    elif row.status in {"result_unknown", "completing"}:
        stage = "result_unknown" if row.status == "result_unknown" else "verifying"
    elif row.status in {"failed", "blocked"}:
        stage = "blocked"
    if attempt is not None and file.storage_state == "stored":
        stages: dict[str, UploadStage] = {
            "pending": "stored",
            "uploading": "uploading",
            "verifying": "verifying",
            "available": "available",
            "blocked": "blocked",
            "result_unknown": "result_unknown",
            "failed": "blocked",
        }
        stage = stages.get(attempt.status, "blocked")
        error_code = public_error(attempt.remote_response.get("error_code"))
    return UploadFileResult(
        material_id=file.id,
        upload_id=row.id,
        file_name=file.file_name,
        byte_size=file.byte_size,
        part_size=row.part_size,
        part_count=(row.expected_size + row.part_size - 1) // row.part_size,
        status=stage,
        received_bytes=file.byte_size if file.storage_state == "stored" else None,
        task_id=row.task_id,
        can_retry=(
            file.storage_state != "stored"
            and (
                row.status == "failed"
                or (row.status == "receiving" and row.error_code == "invalid_part")
            )
        )
        or (
            file.storage_state == "stored"
            and file.byte_size <= settings.MATERIAL_SDK_MAX_UPLOAD_BYTES
            and (
                (
                    attempt is None
                    and row.status == "blocked"
                    and row.error_code
                    in {"no_upload_account", "action_forbidden", "tenant_forbidden"}
                )
                or (
                    attempt is not None
                    and attempt.status in {"failed", "blocked"}
                    and operation is not None
                    and operation.status == "failed"
                    and not operation.attempt_token
                    and not operation.remote_response.get("video_id")
                    and not operation.remote_response.get("mid")
                )
            )
        ),
        error_code=error_code,
        latest_advertiser_id=attempt.advertiser_id if attempt else None,
    )


def _batch_files(
    session: Session, tenant_id: UUID, batch_id: UUID
) -> list[UploadFileResult]:
    rows = session.exec(
        select(ObjectUpload, MaterialFile)
        .join(
            MaterialFile,
            (col(MaterialFile.tenant_id) == ObjectUpload.tenant_id)
            & (col(MaterialFile.id) == ObjectUpload.material_id),
        )
        .where(ObjectUpload.tenant_id == tenant_id, ObjectUpload.batch_id == batch_id)
        .order_by(col(MaterialFile.created_at), col(MaterialFile.id))
        .execution_options(populate_existing=True)
    ).all()
    if not rows:
        return []
    ranked = (
        select(
            MaterialUploadAttempt.id,
            func.row_number()
            .over(
                partition_by=col(MaterialUploadAttempt.material_id),
                order_by=(
                    col(MaterialUploadAttempt.created_at).desc(),
                    col(MaterialUploadAttempt.id).desc(),
                ),
            )
            .label("position"),
        )
        .where(
            MaterialUploadAttempt.tenant_id == tenant_id,
            col(MaterialUploadAttempt.material_id).in_(
                [row.material_id for row, _ in rows]
            ),
        )
        .subquery()
    )
    attempts = session.exec(
        select(MaterialUploadAttempt, MaterialAssetOperation)
        .join(ranked, col(MaterialUploadAttempt.id) == ranked.c.id)
        .join(
            MaterialAssetOperation,
            (col(MaterialAssetOperation.id) == MaterialUploadAttempt.operation_id)
            & (
                col(MaterialAssetOperation.tenant_id) == MaterialUploadAttempt.tenant_id
            ),
        )
        .where(ranked.c.position == 1)
        .execution_options(populate_existing=True)
    ).all()
    latest = {
        attempt.material_id: (attempt, operation) for attempt, operation in attempts
    }
    return [
        _result(row, file, *latest.get(file.id, (None, None))) for row, file in rows
    ]


def get_upload_file_result(
    session: Session, *, context: TenantContext, material_id: UUID
) -> UploadFileResult:
    row = _upload(session, context, material_id, action="read")
    batch = get_upload_batch(session, context=context, batch_id=row.batch_id)
    return next(file for file in batch.files if file.material_id == material_id)


def _aggregate(files: list[UploadFileResult]) -> UploadStage:
    values = {file.status for file in files}
    for state in (
        "result_unknown",
        "blocked",
        "uploading",
        "verifying",
        "receiving",
        "stored",
        "available",
    ):
        if state in values:
            return state
    return "receiving"


def get_upload_batch(
    session: Session, *, context: TenantContext, batch_id: UUID
) -> UploadBatchResult:
    live = require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    batch = session.exec(
        select(UploadBatch)
        .where(UploadBatch.tenant_id == context.tenant_id, UploadBatch.id == batch_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if batch is None:
        raise storage_error("upload_batch_not_found")
    require_bc(session, context=context, bc_id=batch.bc_id, action="read")
    files = _batch_files(session, context.tenant_id, batch_id)
    if live.role == "viewer":
        for file in files:
            file.can_retry = False
    return UploadBatchResult(
        batch_id=batch.id, bc_id=batch.bc_id, status=_aggregate(files), files=files
    )


def refresh_upload_batch(session: Session, *, tenant_id: UUID, batch_id: UUID) -> None:
    """Flush-only progress hook for Task3; caller already owns current authority."""
    session.flush()
    batch = session.exec(
        select(UploadBatch)
        .where(UploadBatch.id == batch_id, UploadBatch.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    batch.status = _aggregate(_batch_files(session, tenant_id, batch_id))
    session.flush()


def _batch_status(session: Session, row: ObjectUpload) -> None:
    refresh_upload_batch(session, tenant_id=row.tenant_id, batch_id=row.batch_id)


def start_upload_batch(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    files: list[UploadFileRequest],
    request_id: UUID,
) -> UploadBatchResult:
    require_bc(session, context=context, bc_id=bc_id, action="upload")
    if not 1 <= len(files) <= 200:
        raise storage_error("invalid_file")
    # Validate again for direct service callers; callers cannot smuggle table fields.
    files = [UploadFileRequest.model_validate(item.model_dump()) for item in files]
    digest = hashlib.sha256(
        json.dumps(
            {"bc_id": bc_id, "files": [item.model_dump() for item in files]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    identity = uuid4()
    result = session.exec(
        insert(UploadBatch)
        .values(
            id=identity,
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            actor_id=context.actor_id,
            request_id=request_id,
            request_digest=digest,
            status="receiving",
            created_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "request_id"])
        .returning(col(UploadBatch.id))
    ).first()
    if result is None:
        batch = session.exec(
            select(UploadBatch).where(
                UploadBatch.tenant_id == context.tenant_id,
                UploadBatch.request_id == request_id,
            )
        ).one()
        if batch.request_digest != digest or batch.actor_id != context.actor_id:
            raise DomainError("idempotency_conflict", "同一上传请求的文件清单不一致")
        return get_upload_batch(session, context=context, batch_id=batch.id)
    for item in files:
        material_id = uuid4()
        key = object_key_for(context.tenant_id, material_id)
        file = MaterialFile(
            id=material_id,
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            file_name=item.file_name,
            object_key=key,
            byte_size=item.size,
            mime_type=item.mime_type,
        )
        session.add(file)
        session.flush()
        session.add(
            ObjectUpload(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                material_id=material_id,
                batch_id=identity,
                object_key=key,
                expected_size=item.size,
                part_size=part_layout(item.size)[0],
            )
        )
    session.flush()
    return get_upload_batch(session, context=context, batch_id=identity)


def _live_claim(row: ObjectUpload) -> bool:
    return (
        row.attempt_token is not None
        and row.claimed_until is not None
        and row.claimed_until > _now()
    )


def _claim(row: ObjectUpload, status: str) -> UUID:
    if _live_claim(row):
        raise storage_error("upload_in_progress")
    token = uuid4()
    row.attempt_token, row.claimed_until, row.status = (
        token,
        _now() + CLAIM_DURATION,
        status,
    )
    row.error_code = None
    return token


def _check_claim(row: ObjectUpload, token: UUID) -> None:
    if (
        row.attempt_token != token
        or not row.claimed_until
        or row.claimed_until <= _now()
    ):
        raise storage_error("upload_in_progress")


def _clear_claim(row: ObjectUpload) -> None:
    row.attempt_token = None
    row.claimed_until = None


def _record_error(
    database_engine: Any,
    context: TenantContext,
    material_id: UUID,
    token: UUID,
    *,
    status: str,
    code: str,
) -> None:
    # Persist only our own claim. Do not require now-revoked authority to record a
    # safe error; credentials and successful objects are never promoted here.
    with Session(database_engine) as session, session.begin():
        row = session.exec(
            select(ObjectUpload)
            .where(
                ObjectUpload.tenant_id == context.tenant_id,
                ObjectUpload.material_id == material_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if row.attempt_token == token:
            row.status, row.error_code = status, code
            _clear_claim(row)
            _batch_status(session, row)


def initialize_object_upload(
    *,
    database_engine: Any,
    context: TenantContext,
    material_id: UUID,
    s3: Any = None,
    bucket: str | None = None,
) -> None:
    with Session(database_engine) as session, session.begin():
        row = _upload(session, context, material_id, lock=True)
        if row.s3_upload_id or row.status == "stored":
            return
        recovering = row.status in {"creating", "result_unknown"}
        if row.status == "blocked":
            raise storage_error("upload_not_retryable")
        if s3 is None:
            settings.require_object_storage()
        token = _claim(row, "creating")
        key, size = row.object_key, row.expected_size
        file = session.get(MaterialFile, material_id)
        assert file is not None
        mime = file.mime_type
    client, owned = (s3, False) if s3 is not None else (None, True)
    try:
        client = client or make_s3()
        bucket = bucket or settings.S3_BUCKET
        remote = None
        if not recovering:
            try:
                result = client.create_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    ContentType=mime,
                    ACL="private",
                    Metadata={
                        "tenant-id": str(context.tenant_id),
                        "material-id": str(material_id),
                        "byte-size": str(size),
                    },
                )
                remote = result.get("UploadId")
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {
                    "AccessDenied",
                    "InvalidBucketName",
                    "NoSuchBucket",
                }:
                    _record_error(
                        database_engine,
                        context,
                        material_id,
                        token,
                        status="failed",
                        code="object_storage_unavailable",
                    )
                    raise storage_error(
                        "object_storage_unavailable", retryable=True
                    ) from None
            except BotoCoreError:
                pass
        if not isinstance(remote, str) or not remote:
            remote = find_multipart(client, bucket=bucket, key=key)
        if not remote:
            raise storage_error("object_result_unknown")
        with Session(database_engine) as session, session.begin():
            row = _upload(session, context, material_id, lock=True)
            _check_claim(row, token)
            row.s3_upload_id, row.status = remote, "receiving"
            _clear_claim(row)
            _batch_status(session, row)
    except DomainError as error:
        if error.code != "object_storage_unavailable":
            _record_error(
                database_engine,
                context,
                material_id,
                token,
                status="result_unknown",
                code="object_result_unknown",
            )
        raise
    except BotoCoreError, ClientError:
        _record_error(
            database_engine,
            context,
            material_id,
            token,
            status="result_unknown",
            code="object_result_unknown",
        )
        raise storage_error("object_result_unknown") from None
    finally:
        if owned and client is not None:
            client.close()


def sign_upload_part(
    *,
    database_engine: Any,
    context: TenantContext,
    material_id: UUID,
    part_number: int,
    s3: Any = None,
    bucket: str | None = None,
) -> SignedPart:
    # Validate scope and part range before any multipart creation.
    with Session(database_engine) as session:
        row = _upload(session, context, material_id)
        count = (row.expected_size + row.part_size - 1) // row.part_size
        if type(part_number) is not int or not 1 <= part_number <= count:
            raise storage_error("invalid_part")
    # Authority is reloaded after creation/recovery before signing.
    initialize_object_upload(
        database_engine=database_engine,
        context=context,
        material_id=material_id,
        s3=s3,
        bucket=bucket,
    )
    with Session(database_engine) as session:
        row = _upload(session, context, material_id)
        if row.status != "receiving" or not row.s3_upload_id:
            raise storage_error("upload_not_ready")
        count = (row.expected_size + row.part_size - 1) // row.part_size
        if type(part_number) is not int or not 1 <= part_number <= count:
            raise storage_error("invalid_part")
        key, remote = row.object_key, row.s3_upload_id
    client = s3 if s3 is not None else make_s3()
    try:
        return SignedPart(
            url=sign_part(
                client,
                bucket=bucket or settings.S3_BUCKET,
                key=key,
                upload_id=remote,
                part_number=part_number,
            )
        )
    finally:
        if s3 is None:
            client.close()


def _parts(parts: list[UploadedPart], expected_count: int) -> list[dict[str, Any]]:
    values = sorted(
        [UploadedPart.model_validate(item.model_dump()) for item in parts],
        key=lambda item: item.part_number,
    )
    if [item.part_number for item in values] != list(range(1, expected_count + 1)):
        raise storage_error("invalid_part")
    return [{"PartNumber": item.part_number, "ETag": item.etag} for item in values]


def finish_upload(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    parts: list[UploadedPart],
) -> UUID:
    """Flush-only finalizer; only the server's verified phase may call this."""
    row = _upload(session, context, material_id, lock=True)
    expected = _parts(parts, (row.expected_size + row.part_size - 1) // row.part_size)
    if row.parts != expected:
        raise DomainError("idempotency_conflict", "完成请求的分片清单不一致")
    if row.status == "stored" and row.task_id:
        return row.task_id
    if row.status != "object_verified":
        raise storage_error("upload_not_ready")
    file = session.get(MaterialFile, material_id, populate_existing=True)
    assert file is not None
    file.storage_state = "stored"
    # Stable initiator prevents an authorized colleague's recovery from changing
    # an existing outbox identity. The worker revalidates that actor itself.
    batch = session.get(UploadBatch, row.batch_id)
    assert batch is not None
    initiator = TenantContext(
        tenant_id=context.tenant_id, actor_id=batch.actor_id, role=context.role
    )
    row.task_id = enqueue_after_commit(
        session,
        context=initiator,
        task_name="materials.upload_original",
        task_key=f"upload-original:{material_id}",
        payload={"material_id": str(material_id)},
    )
    row.status, row.error_code = "stored", None
    _clear_claim(row)
    _batch_status(session, row)
    session.flush()
    return row.task_id


def complete_object_upload(
    *,
    database_engine: Any,
    context: TenantContext,
    material_id: UUID,
    parts: list[UploadedPart],
    s3: Any = None,
    bucket: str | None = None,
) -> UUID:
    with Session(database_engine) as session, session.begin():
        row = _upload(session, context, material_id, lock=True)
        values = _parts(parts, (row.expected_size + row.part_size - 1) // row.part_size)
        if row.parts and row.parts != values:
            raise DomainError("idempotency_conflict", "完成请求的分片清单不一致")
        if row.task_id:
            return row.task_id
        if (
            row.status not in {"receiving", "completing", "result_unknown"}
            or not row.s3_upload_id
        ):
            raise storage_error("upload_not_ready")
        recovering = row.status != "receiving"
        if s3 is None:
            settings.require_object_storage()
        token = _claim(row, "completing")
        row.parts = values
        key, remote, size = row.object_key, row.s3_upload_id, row.expected_size
        _batch_status(session, row)
    client = None
    try:
        client = s3 if s3 is not None else make_s3()
        bucket = bucket or settings.S3_BUCKET
        if not recovering:
            try:
                client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=remote,
                    MultipartUpload={"Parts": values},
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {
                    "InvalidPart",
                    "InvalidPartOrder",
                    "EntityTooSmall",
                }:
                    with Session(database_engine) as session, session.begin():
                        row = _upload(session, context, material_id, lock=True)
                        _check_claim(row, token)
                        row.parts, row.status, row.error_code = (
                            [],
                            "receiving",
                            "invalid_part",
                        )
                        _clear_claim(row)
                        _batch_status(session, row)
                    raise storage_error("invalid_part", retryable=True) from None
                # NoSuchUpload, timeout and all other uncertain completion results
                # require HEAD evidence. Never replay a completing operation.
            except BotoCoreError:
                pass
        head_verified(
            client,
            bucket=bucket,
            key=key,
            tenant_id=context.tenant_id,
            material_id=material_id,
            expected_size=size,
        )
        with Session(database_engine) as session, session.begin():
            row = _upload(session, context, material_id, lock=True)
            _check_claim(row, token)
            row.status = "object_verified"
            session.flush()
            return finish_upload(
                session, context=context, material_id=material_id, parts=parts
            )
    except DomainError as error:
        if error.code != "invalid_part":
            _record_error(
                database_engine,
                context,
                material_id,
                token,
                status="blocked"
                if error.code in {"incomplete_object", "object_identity_unverified"}
                else "result_unknown",
                code=error.code
                if error.code in {"incomplete_object", "object_identity_unverified"}
                else "object_result_unknown",
            )
        raise
    except BotoCoreError, ClientError:
        _record_error(
            database_engine,
            context,
            material_id,
            token,
            status="result_unknown",
            code="object_result_unknown",
        )
        raise storage_error("object_result_unknown") from None
    finally:
        if s3 is None and client is not None:
            client.close()


def retry_object_upload(
    *,
    database_engine: Any,
    context: TenantContext,
    material_id: UUID,
    s3: Any = None,
    bucket: str | None = None,
) -> UploadFileResult:
    """Retry only a definitively rejected object operation, never a platform send."""
    with Session(database_engine) as session:
        row = _upload(session, context, material_id)
        file = session.get(MaterialFile, material_id)
        assert file is not None
        if not _result(row, file).can_retry:
            raise storage_error("upload_not_retryable")
    initialize_object_upload(
        database_engine=database_engine,
        context=context,
        material_id=material_id,
        s3=s3,
        bucket=bucket,
    )
    with Session(database_engine) as session:
        row = _upload(session, context, material_id)
        file = session.get(MaterialFile, material_id)
        assert file is not None
        return _result(row, file)
