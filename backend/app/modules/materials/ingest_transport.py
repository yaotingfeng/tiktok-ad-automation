"""Generation-fenced multipart coordination with bounded control-plane RPCs.

Each network phase closes its transaction first. A request lists at most one
100-part page or signs at most 2 parts. Socket timeouts are not a DNS/process
hard limit; large byte reads and provider ingestion belong to bounded workers.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

from . import ingest_service as service
from . import storage
from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
    canonical_object_key,
    record_milestone,
    transition_ingest_file,
)
from .ingest_schemas import (
    IngestFilePublic,
    IngestIdentity,
    IngestPart,
    IngestPartsPage,
    IngestPartUrl,
    IngestPartUrls,
    IngestPartUrlsCreate,
)
from .models import AccountMaterial, MaterialAssetOperation, MaterialFile
from .object_budget import mark_object_stored, reserve_object
from .object_uses import release_object_uses
from .object_validation import enqueue_validation
from .repository import decode_material_cursor, encode_material_cursor

register_dispatch_task("materials.reconcile_ingest_transport", "resources")
register_dispatch_task("materials.repair_ingest_transports", "control")
CLAIM_SECONDS = 180
TRANSPORT_HARD_LIMIT = 150
RECOVERY_ERRORS = {
    "multipart_creating",
    "multipart_create_unknown",
    "multipart_collecting",
    "multipart_completing",
    "multipart_complete_unknown",
}
RECOVERY_PREDICATE = "temporary_material_object.error_code IN ('multipart_creating','multipart_create_unknown','multipart_collecting','multipart_completing','multipart_complete_unknown')"
TERMINAL_RECEIVED = {
    "stored",
    "validating",
    "verified",
    "cleanup_pending",
    "deleting",
    "delete_unknown",
    "deleted",
    "missing",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _locked(
    db: Session,
    *,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    authorize: bool = True,
    check_revision: bool = True,
    allow_unknown_upload: bool = False,
) -> tuple[MaterialFile, TemporaryMaterialObject, IngestSessionFile]:
    parent = (
        service.get_session(db, context=context, session_id=session_id, action="upload")
        if authorize
        else None
    )
    file = db.exec(
        select(MaterialFile)
        .where(
            col(MaterialFile.tenant_id) == context.tenant_id,
            col(MaterialFile.id) == material_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if file is None:
        raise DomainError("material_not_found", "素材不存在或不可见")
    obj = db.exec(
        select(TemporaryMaterialObject)
        .where(
            col(TemporaryMaterialObject.tenant_id) == context.tenant_id,
            col(TemporaryMaterialObject.bc_id) == file.bc_id,
            col(TemporaryMaterialObject.material_id) == material_id,
            col(TemporaryMaterialObject.generation) == identity.generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    row = db.exec(
        select(IngestSessionFile)
        .where(
            col(IngestSessionFile.tenant_id) == context.tenant_id,
            col(IngestSessionFile.bc_id) == file.bc_id,
            col(IngestSessionFile.session_id) == session_id,
            col(IngestSessionFile.material_id) == material_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if obj is None or row is None or (parent and parent.bc_id != file.bc_id):
        raise DomainError("material_not_found", "素材代次不存在或不可见")
    if (
        file.current_object_generation != identity.generation
        or row.current_generation != identity.generation
    ):
        raise DomainError("version_conflict", "素材上传代次已更新")
    if identity.upload_id != obj.s3_upload_id and not (
        allow_unknown_upload and identity.upload_id is None
    ):
        raise DomainError("version_conflict", "分片上传身份已更新")
    if check_revision and identity.operation_revision != obj.revision:
        raise DomainError("version_conflict", "素材操作版本已更新")
    return file, obj, row


def _token_locked(
    db: Session,
    *,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    nonce: UUID,
) -> tuple[MaterialFile, TemporaryMaterialObject, IngestSessionFile]:
    result = _locked(
        db,
        context=context,
        session_id=session_id,
        material_id=material_id,
        identity=identity,
        authorize=False,
        check_revision=False,
        allow_unknown_upload=True,
    )
    if result[1].claim_token != nonce:
        raise DomainError("version_conflict", "本次素材操作已失去认领")
    return result


def _claim(obj: TemporaryMaterialObject, operation: str) -> UUID:
    nonce = uuid4()
    obj.revision += 1
    obj.claim_token = nonce
    obj.claimed_until = _now() + timedelta(seconds=CLAIM_SECONDS)
    obj.next_attempt_at = obj.claimed_until
    obj.error_code = operation
    return nonce


def _clear_claim(obj: TemporaryMaterialObject) -> None:
    obj.claim_token = None
    obj.claimed_until = None


def _busy(obj: TemporaryMaterialObject) -> bool:
    return bool(obj.claim_token and obj.claimed_until and obj.claimed_until > _now())


def _snapshot(obj: TemporaryMaterialObject) -> TemporaryMaterialObject:
    return TemporaryMaterialObject(**obj.model_dump())


def _close(client: Any, *, owned: bool) -> None:
    if owned and client is not None:
        try:
            client.close()
        except Exception:
            # Receipt/state has already been preserved. Never expose transport
            # cleanup text or reinterpret a successful remote operation.
            pass


def _read(
    database_engine: Any, context: TenantContext, session_id: UUID, material_id: UUID
) -> IngestFilePublic:
    with Session(database_engine) as db:
        return service.read_file(
            db, context=context, session_id=session_id, material_id=material_id
        )


def _metadata(obj: TemporaryMaterialObject) -> dict[str, str]:
    if not obj.bc_id.isascii() or any(
        ord(char) < 32 or ord(char) == 127 for char in obj.bc_id
    ):
        raise DomainError("object_identity_unverified", "BC 标识不能用于对象元数据")
    return {
        "tenant-id": str(obj.tenant_id),
        "bc-id": obj.bc_id,
        "material-id": str(obj.material_id),
        "generation": str(obj.generation),
    }


def _head(client: Any, obj: TemporaryMaterialObject, mime_type: str) -> dict[str, Any]:
    result = client.head_object(Bucket=obj.storage_bucket, Key=obj.object_key)
    if (
        not isinstance(result, dict)
        or type(result.get("ContentLength")) is not int
        or result["ContentLength"] != obj.expected_bytes
    ):
        raise DomainError("incomplete_object", "原件接收长度不一致")
    metadata = result.get("Metadata")
    if (
        not isinstance(metadata, dict)
        or any(metadata.get(key) != value for key, value in _metadata(obj).items())
        or result.get("ContentType") != mime_type
    ):
        raise DomainError(
            "object_identity_unverified", "对象归属、代次或媒体类型不一致"
        )
    return result


def _mark_failure(
    database_engine: Any,
    *,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    nonce: UUID,
    code: str,
) -> None:
    with Session(database_engine) as db, db.begin():
        _, obj, _ = _token_locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            nonce=nonce,
        )
        obj.error_code = code
        obj.next_attempt_at = _now() + timedelta(seconds=30)
        _clear_claim(obj)


def resume_file(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    s3: Any = None,
) -> IngestFilePublic:
    with Session(database_engine) as db, db.begin():
        file, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            check_revision=False,
            allow_unknown_upload=True,
        )
        recovering = obj.error_code in {
            "multipart_creating",
            "multipart_create_unknown",
        }
        service.require_ingest_storage(
            reconciliation=recovering
            or bool(obj.s3_upload_id)
            or obj.received_at is not None
        )
        if identity.operation_revision > obj.revision:
            raise DomainError("version_conflict", "素材操作版本无效")
        cancelled_recovery = (
            row.error_code == "user_cancelled"
            and obj.status == "cleanup_pending"
            and recovering
        )
        if not cancelled_recovery and (
            row.error_code == "user_cancelled"
            or obj.status
            in {
                "cleanup_pending",
                "deleting",
                "delete_unknown",
                "deleted",
                "missing",
            }
        ):
            return service.file_public(row, file, obj)
        if obj.s3_upload_id or _busy(obj):
            return service.file_public(row, file, obj)
        if recovering:
            if (
                obj.reserved_bytes != obj.expected_bytes
                or obj.reservation_released_at is not None
            ):
                raise DomainError("object_budget_corrupt", "暂存预留需要核查")
        elif not reserve_object(
            db, context=context, object_id=obj.id, byte_size=obj.expected_bytes
        ):
            return service.file_public(row, file, obj)
        nonce = _claim(obj, "multipart_creating")
        snapshot, mime = _snapshot(obj), file.mime_type
    client, sent = s3, False
    try:
        client = client if client is not None else storage.make_object_s3(snapshot)
        if recovering:
            response = client.list_multipart_uploads(
                Bucket=snapshot.storage_bucket,
                Prefix=snapshot.object_key,
                MaxUploads=100,
            )
            entries = (
                response.get("Uploads", []) if isinstance(response, dict) else None
            )
            if (
                not isinstance(entries, list)
                or len(entries) > 100
                or response.get("IsTruncated") is not False
            ):
                raise DomainError("object_result_unknown", "分片创建结果仍待核实")
            candidates = [
                entry.get("UploadId")
                for entry in entries
                if isinstance(entry, dict) and entry.get("Key") == snapshot.object_key
            ]
            if len(candidates) != 1:
                raise DomainError("object_result_unknown", "分片创建结果仍待核实")
            remote = candidates[0]
        else:
            values = _metadata(snapshot)
            sent = True
            result = client.create_multipart_upload(
                Bucket=snapshot.storage_bucket,
                Key=snapshot.object_key,
                ContentType=mime,
                Metadata=values,
            )
            remote = result.get("UploadId") if isinstance(result, dict) else None
        if not isinstance(remote, str) or not remote.strip() or len(remote) > 512:
            raise DomainError("object_result_unknown", "分片创建结果仍待核实")
        with Session(database_engine) as db, db.begin():
            _, obj, row = _token_locked(
                db,
                context=context,
                session_id=session_id,
                material_id=material_id,
                identity=identity,
                nonce=nonce,
            )
            obj.s3_upload_id = remote
            cancelled = row.error_code == "user_cancelled"
            obj.status = "cleanup_pending" if cancelled else "receiving"
            obj.error_code = None
            _clear_claim(obj)
            transition_ingest_file(
                db,
                tenant_id=context.tenant_id,
                file_id=row.id,
                expected_revision=row.revision,
                status="cancelled" if cancelled else "receiving",
                error_code="user_cancelled" if cancelled else None,
            )
    except SoftTimeLimitExceeded:
        raise
    except Exception as error:
        code = (
            "multipart_create_unknown"
            if sent or recovering
            else "object_storage_unavailable"
        )
        _mark_failure(
            database_engine,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            nonce=nonce,
            code=code,
        )
        if isinstance(error, DomainError) and not sent and not recovering:
            raise error from None
        raise DomainError(
            "object_result_unknown"
            if sent or recovering
            else "object_storage_unavailable",
            "分片创建结果待核实",
        ) from None
    finally:
        _close(client, owned=s3 is None)
    return _read(database_engine, context, session_id, material_id)


def _parts_scope(
    context: TenantContext, session_id: UUID, obj: TemporaryMaterialObject
) -> dict[str, str]:
    return {
        "kind": "ingest_parts",
        "tenant": str(context.tenant_id),
        "bc": obj.bc_id,
        "session": str(session_id),
        "material": str(obj.material_id),
        "generation": str(obj.generation),
        "upload": obj.s3_upload_id or "",
        "revision": str(obj.revision),
    }


def _part_bytes(obj: TemporaryMaterialObject, number: int) -> int:
    count = (obj.expected_bytes + obj.part_size - 1) // obj.part_size
    if type(number) is not int or not 1 <= number <= count:
        raise DomainError("invalid_part", "分片序号无效")
    return min(obj.part_size, obj.expected_bytes - (number - 1) * obj.part_size)


def _receiving(obj: TemporaryMaterialObject, row: IngestSessionFile) -> None:
    if (
        obj.status != "receiving"
        or not obj.s3_upload_id
        or obj.reservation_released_at
        or obj.reserved_bytes != obj.expected_bytes
        or row.error_code == "user_cancelled"
        or obj.error_code
        in {
            "multipart_collecting",
            "multipart_completing",
            "multipart_complete_unknown",
        }
    ):
        raise DomainError("upload_not_ready", "原件当前不可继续接收")


def sign_parts(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestPartUrlsCreate,
    s3: Any = None,
) -> IngestPartUrls:
    service.require_ingest_storage()
    ttl = service.settings.MATERIAL_PART_URL_SECONDS
    with Session(database_engine) as db, db.begin():
        _, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
        )
        _receiving(obj, row)
        sizes = [(number, _part_bytes(obj, number)) for number in identity.part_numbers]
        from .part_receipts import signing_window, verify_signed_deadline

        uses = signing_window(
            db,
            context=context,
            obj=obj,
            request_id=identity.request_id,
            numbers=identity.part_numbers,
            ttl=ttl,
        )
        permissions = {
            use.completion_evidence["part_number"]: (
                use.id,
                use.nonce,
                use.revision,
                use.expires_at,
            )
            for use in uses
        }
        snapshot = _snapshot(obj)
    assert snapshot.storage_bucket is not None and snapshot.s3_upload_id is not None
    client = s3
    try:
        client = client if client is not None else storage.make_object_s3(snapshot)
        items = []
        for number, size in sizes:
            expires_in = int((permissions[number][3] - _now()).total_seconds())
            if expires_in < 60:
                raise DomainError(
                    "part_permission_expired", "分片签名已到期，请恢复上传窗口"
                )
            url = storage.sign_part(
                client,
                bucket=snapshot.storage_bucket,
                key=snapshot.object_key,
                upload_id=snapshot.s3_upload_id,
                part_number=number,
                byte_size=size,
                expires_in=expires_in,
            )
            verify_signed_deadline(url, permissions[number][3])
            items.append(
                IngestPartUrl(
                    part_number=number,
                    byte_size=size,
                    expires_in=expires_in,
                    permission_id=permissions[number][0],
                    permission_nonce=permissions[number][1],
                    permission_revision=permissions[number][2],
                    url=url,
                )
            )
        with Session(database_engine) as db:
            _, obj, row = _locked(
                db,
                context=context,
                session_id=session_id,
                material_id=material_id,
                identity=identity,
            )
            _receiving(obj, row)
        return IngestPartUrls(
            generation=snapshot.generation,
            upload_id=snapshot.s3_upload_id,
            operation_revision=snapshot.revision,
            items=items,
        )
    finally:
        _close(client, owned=s3 is None)


def _parse_parts(
    response: Any, obj: TemporaryMaterialObject, *, marker: int, limit: int
) -> tuple[list[IngestPart], int | None]:
    if not isinstance(response, dict) or type(response.get("IsTruncated")) is not bool:
        raise DomainError("object_identity_unverified", "分片列表结构无效")
    raw = response.get("Parts", [])
    if not isinstance(raw, list) or len(raw) > limit:
        raise DomainError("object_identity_unverified", "分片列表未按页返回")
    result = []
    previous = marker
    for part in raw:
        if not isinstance(part, dict):
            raise DomainError("object_identity_unverified", "分片列表结构无效")
        number, size, etag = part.get("PartNumber"), part.get("Size"), part.get("ETag")
        if (
            type(number) is not int
            or number <= previous
            or type(size) is not int
            or size != _part_bytes(obj, number)
            or not isinstance(etag, str)
            or not etag.strip()
            or len(etag) > 256
            or any(ord(char) < 32 for char in etag)
        ):
            raise DomainError("incomplete_object", "分片大小或接收证据不一致")
        result.append(IngestPart(part_number=number, byte_size=size, etag=etag))
        previous = number
    next_marker = None
    if response["IsTruncated"]:
        candidate = response.get("NextPartNumberMarker")
        if not result or type(candidate) is not int or candidate != previous:
            raise DomainError("object_result_unknown", "分片游标尚未得到完整证据")
        next_marker = candidate
    return result, next_marker


def list_parts(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    cursor: str | None = None,
    limit: int = 100,
    s3: Any = None,
) -> IngestPartsPage:
    service.require_ingest_storage()
    if not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "分片页大小无效")
    with Session(database_engine) as db:
        _, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
        )
        _receiving(obj, row)
        snapshot = _snapshot(obj)
    scope, marker = _parts_scope(context, session_id, snapshot), 0
    if cursor:
        value, cursor_object = decode_material_cursor(cursor, scope=scope)
        try:
            marker = int(value)
            if cursor_object != snapshot.id or not 0 < marker <= 10_000:
                raise ValueError
        except ValueError:
            raise DomainError("invalid_cursor", "分片游标无效") from None
    client = s3
    try:
        client = client if client is not None else storage.make_object_s3(snapshot)
        response = client.list_parts(
            Bucket=snapshot.storage_bucket,
            Key=snapshot.object_key,
            UploadId=snapshot.s3_upload_id,
            PartNumberMarker=marker,
            MaxParts=limit,
        )
        parts, following = _parse_parts(response, snapshot, marker=marker, limit=limit)
        with Session(database_engine) as db:
            _locked(
                db,
                context=context,
                session_id=session_id,
                material_id=material_id,
                identity=identity,
            )
        assert snapshot.s3_upload_id is not None
        return IngestPartsPage(
            generation=snapshot.generation,
            upload_id=snapshot.s3_upload_id,
            operation_revision=snapshot.revision,
            items=parts,
            next_cursor=encode_material_cursor(
                scope=scope, name=str(following), identity=snapshot.id
            )
            if following
            else None,
        )
    except BotoCoreError, ClientError:
        raise DomainError("object_storage_unavailable", "暂时无法核对分片") from None
    finally:
        _close(client, owned=s3 is None)


def _stored(
    database_engine: Any,
    *,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    nonce: UUID,
) -> None:
    with Session(database_engine) as db, db.begin():
        file, obj, row = _token_locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            nonce=nonce,
        )
        cancelled = row.error_code == "user_cancelled"
        if cancelled and obj.status == "cleanup_pending":
            # Exact successful Complete + HEAD is new receipt evidence. Keep the
            # intake closed after accounting; do not dispatch cancelled work.
            obj.status = "receiving"
        mark_object_stored(
            db, context=context, object_id=obj.id, actual_bytes=obj.expected_bytes
        )
        file.storage_state = "stored"
        obj.error_code = None
        _clear_claim(obj)
        release_object_uses(db, object_id=obj.id, purpose="part_put")
        if cancelled:
            obj.status = "cleanup_pending"
            record_milestone(
                db,
                tenant_id=obj.tenant_id,
                bc_id=obj.bc_id,
                session_id=row.session_id,
                material_id=obj.material_id,
                milestone="uploaded",
            )
        else:
            enqueue_validation(db, context=context, object_id=obj.id)


def complete_file(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
    s3: Any = None,
) -> IngestFilePublic:
    with Session(database_engine) as db, db.begin():
        file, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            check_revision=False,
        )
        recovering = obj.error_code in {
            "multipart_completing",
            "multipart_complete_unknown",
        }
        service.require_ingest_storage(
            reconciliation=recovering or obj.received_at is not None
        )
        if obj.received_at is not None:
            return service.file_public(row, file, obj)
        cancelled_recovery = (
            row.error_code == "user_cancelled"
            and obj.status == "cleanup_pending"
            and recovering
        )
        if not obj.s3_upload_id or (
            not cancelled_recovery
            and (obj.status != "receiving" or row.error_code == "user_cancelled")
        ):
            raise DomainError("upload_not_ready", "原件当前不可完成")
        if identity.operation_revision > obj.revision:
            raise DomainError("version_conflict", "素材操作版本无效")
        if identity.operation_revision != obj.revision or _busy(obj):
            return service.file_public(row, file, obj)
        nonce = _claim(
            obj, "multipart_complete_unknown" if recovering else "multipart_collecting"
        )
        snapshot, mime = _snapshot(obj), file.mime_type
    client, armed = s3, recovering
    try:
        client = client if client is not None else storage.make_object_s3(snapshot)
        if not recovering:
            marker = snapshot.parts[-1]["part_number"] if snapshot.parts else 0
            response = client.list_parts(
                Bucket=snapshot.storage_bucket,
                Key=snapshot.object_key,
                UploadId=snapshot.s3_upload_id,
                PartNumberMarker=marker,
                MaxParts=100,
            )
            parts, following = _parse_parts(
                response, snapshot, marker=marker, limit=100
            )
            collected = snapshot.parts + [part.model_dump() for part in parts]
            with Session(database_engine) as db, db.begin():
                file, obj, row = _token_locked(
                    db,
                    context=context,
                    session_id=session_id,
                    material_id=material_id,
                    identity=identity,
                    nonce=nonce,
                )
                if row.status == "cancelled" or obj.status != "receiving":
                    # ListParts was read-only. A cancel during that RPC must
                    # never acquire new Complete authority on its late return.
                    _clear_claim(obj)
                    obj.error_code = None
                    return service.file_public(row, file, obj)
                obj.parts = collected
                if following is not None:
                    _clear_claim(obj)
                    obj.next_attempt_at = _now()
                else:
                    expected = (obj.expected_bytes + obj.part_size - 1) // obj.part_size
                    if [part["part_number"] for part in collected] != list(
                        range(1, expected + 1)
                    ):
                        obj.parts, obj.error_code = [], None
                        _clear_claim(obj)
                    else:
                        # Durable send fence before the only completion POST.
                        service.get_session(
                            db, context=context, session_id=session_id, action="upload"
                        )
                        obj.error_code = "multipart_completing"
                        armed = True
            if following is not None:
                return _read(database_engine, context, session_id, material_id)
            if not armed:
                raise DomainError("incomplete_object", "文件仍有未接收的分片")
            client.complete_multipart_upload(
                Bucket=snapshot.storage_bucket,
                Key=snapshot.object_key,
                UploadId=snapshot.s3_upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part["part_number"], "ETag": part["etag"]}
                        for part in collected
                    ]
                },
            )
        _head(client, snapshot, mime)
        _stored(
            database_engine,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            nonce=nonce,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as error:
        if (
            not armed
            and isinstance(error, DomainError)
            and error.code == "incomplete_object"
        ):
            raise error from None
        _mark_failure(
            database_engine,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            nonce=nonce,
            code="multipart_complete_unknown" if armed else "multipart_collecting",
        )
        raise DomainError(
            "object_result_unknown" if armed else "object_storage_unavailable",
            "原件接收结果仍需核实",
        ) from None
    finally:
        _close(client, owned=s3 is None)
    return _read(database_engine, context, session_id, material_id)


def cancel_file(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
) -> IngestFilePublic:
    with Session(database_engine) as db, db.begin():
        file, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
        )
        row.error_code = "user_cancelled"
        if row.status not in {"available", "uploading", "verifying", "result_unknown"}:
            transition_ingest_file(
                db,
                tenant_id=context.tenant_id,
                file_id=row.id,
                expected_revision=row.revision,
                status="cancelled",
                error_code="user_cancelled",
            )
        untouched = (
            obj.status == "waiting_capacity"
            and obj.reserved_at is None
            and obj.s3_upload_id is None
            and obj.received_at is None
            and obj.error_code is None
        )
        if untouched:
            obj.revision += 1
        elif obj.status != "deleted":
            # In-flight claim/send facts stay intact for recovery and eligibility.
            obj.status = "cleanup_pending"
            obj.revision += 1
            cleanup = ObjectCleanup(
                tenant_id=context.tenant_id,
                bc_id=obj.bc_id,
                material_id=material_id,
                generation=obj.generation,
                reason="user_cancelled",
                eligibility_evidence={
                    "actor_id": str(context.actor_id),
                    "cancelled_at": _now().isoformat(),
                    "generation": obj.generation,
                },
            )
            db.exec(
                insert(ObjectCleanup)
                .values(cleanup.model_dump())
                .on_conflict_do_nothing(constraint="uq_object_cleanup_generation")
            )
        result = service.file_public(row, file, obj)
    return result


def new_generation(
    *,
    database_engine: Any,
    context: TenantContext,
    session_id: UUID,
    material_id: UUID,
    identity: IngestIdentity,
) -> IngestFilePublic:
    service.require_ingest_storage()
    with Session(database_engine) as db, db.begin():
        file, obj, row = _locked(
            db,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
        )
        safe = obj.status == "deleted" and obj.reservation_released_at is not None
        untouched = (
            obj.status == "waiting_capacity"
            and obj.reserved_at is None
            and obj.s3_upload_id is None
            and obj.received_at is None
            and obj.error_code is None
        )
        remote = db.exec(
            select(col(MaterialAssetOperation.id))
            .where(
                col(MaterialAssetOperation.tenant_id) == context.tenant_id,
                col(MaterialAssetOperation.material_id) == material_id,
                col(MaterialAssetOperation.status).in_(
                    [
                        "pending",
                        "sending",
                        "result_unknown",
                        "verifying",
                        "confirmed_absent",
                    ]
                ),
            )
            .limit(1)
        ).first()
        available = db.exec(
            select(col(AccountMaterial.id))
            .where(
                col(AccountMaterial.tenant_id) == context.tenant_id,
                col(AccountMaterial.material_id) == material_id,
                col(AccountMaterial.status) == "available",
            )
            .limit(1)
        ).first()
        uses = db.exec(
            select(col(OriginalUse.id))
            .where(
                col(OriginalUse.tenant_id) == context.tenant_id,
                col(OriginalUse.material_id) == material_id,
                col(OriginalUse.generation) == obj.generation,
                col(OriginalUse.status) == "active",
            )
            .limit(1)
        ).first()
        if not (safe or untouched) or remote or available or uses or _busy(obj):
            raise DomainError(
                "material_retry_not_allowed", "前代原件或平台操作尚未安全结束"
            )
        generation = obj.generation + 1
        new = TemporaryMaterialObject(
            tenant_id=context.tenant_id,
            bc_id=obj.bc_id,
            material_id=material_id,
            generation=generation,
            object_key=canonical_object_key(
                context.tenant_id, obj.bc_id, material_id, generation
            ),
            expected_bytes=row.byte_size,
            part_size=storage.part_layout(row.byte_size)[0],
            storage_provider=service.settings.OBJECT_STORAGE_PROVIDER,
            storage_endpoint=service.settings.S3_ENDPOINT_URL,
            storage_bucket=service.settings.S3_BUCKET,
        )
        db.add(new)
        file.current_object_generation = row.current_generation = generation
        file.storage_state = "receiving"
        row.dispatch_id, row.error_code = None, None
        transition_ingest_file(
            db,
            tenant_id=context.tenant_id,
            file_id=row.id,
            expected_revision=row.revision,
            status="waiting_capacity",
        )
        db.flush()
        return service.file_public(row, file, new)


def repair_ingest_transports(db: Session, *, limit: int = 100) -> int:
    """Bounded indexed recovery selection; SKIP LOCKED preserves worker order.

    The durable phase marker is the recovery intent. Creating a dispatch here
    repairs API death before a known full object's validator could be enqueued.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "恢复窗口必须在1到100之间")
    now = _now()
    identities = db.exec(
        select(col(TemporaryMaterialObject.id))
        .where(
            text(RECOVERY_PREDICATE),
            col(TemporaryMaterialObject.status).in_(
                ["reserved", "receiving", "cleanup_pending"]
            ),
            col(TemporaryMaterialObject.next_attempt_at) <= now,
            or_(
                col(TemporaryMaterialObject.claimed_until).is_(None),
                col(TemporaryMaterialObject.claimed_until) <= now,
            ),
        )
        .order_by(
            col(TemporaryMaterialObject.status),
            col(TemporaryMaterialObject.next_attempt_at),
            col(TemporaryMaterialObject.id),
        )
        .limit(limit)
    ).all()
    enqueued = 0
    for object_id in identities:
        candidate = db.get(TemporaryMaterialObject, object_id)
        if candidate is None:
            continue
        file = db.exec(
            select(MaterialFile)
            .where(
                col(MaterialFile.id) == candidate.material_id,
                col(MaterialFile.tenant_id) == candidate.tenant_id,
            )
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if file is None:
            continue
        obj = db.exec(
            select(TemporaryMaterialObject)
            .where(col(TemporaryMaterialObject.id) == object_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            obj is None
            or _busy(obj)
            or obj.error_code not in RECOVERY_ERRORS
            or obj.next_attempt_at > now
            or obj.generation != file.current_object_generation
        ):
            continue
        row = db.exec(
            select(IngestSessionFile).where(
                col(IngestSessionFile.tenant_id) == obj.tenant_id,
                col(IngestSessionFile.bc_id) == obj.bc_id,
                col(IngestSessionFile.material_id) == obj.material_id,
                col(IngestSessionFile.current_generation) == obj.generation,
            )
        ).one_or_none()
        if row is None or (
            row.error_code == "user_cancelled"
            and obj.error_code == "multipart_collecting"
        ):
            continue
        parent = db.get(IngestSession, row.session_id)
        assert parent is not None
        context = TenantContext(
            tenant_id=obj.tenant_id, actor_id=parent.actor_id, role="operator"
        )
        task_key = f"ingest-reconcile:{obj.id}:{obj.revision}"
        previous_dispatch = db.exec(
            select(col(PendingDispatch.id)).where(
                col(PendingDispatch.tenant_id) == obj.tenant_id,
                col(PendingDispatch.task_key) == task_key,
            )
        ).one_or_none()
        dispatch_id = enqueue_after_commit(
            db,
            context=context,
            task_name="materials.reconcile_ingest_transport",
            task_key=task_key,
            payload={
                "object_id": str(obj.id),
                "generation": obj.generation,
                "revision": obj.revision,
            },
        )
        dispatch = db.exec(
            select(PendingDispatch)
            .where(
                col(PendingDispatch.id) == dispatch_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if previous_dispatch is None:
            enqueued += 1
        elif (
            dispatch.published_at is not None
            and dispatch.published_at <= now - timedelta(seconds=120)
        ):
            dispatch.published_at = None
            dispatch.available_at = max(dispatch.available_at, now)
            enqueued += 1
        # Unpublished rows belong to the outbox, including its broker backoff.
        # A periodic business repair must not reset attempts or available_at.
        obj.next_attempt_at = now + timedelta(seconds=120)
    return enqueued


def reconcile_ingest_transport(
    *,
    database_engine: Any,
    context: TenantContext,
    object_id: UUID,
    generation: int,
    revision: int,
    s3: Any = None,
) -> None:
    with Session(database_engine) as db:
        obj = db.get(TemporaryMaterialObject, object_id)
        if (
            obj is None
            or obj.tenant_id != context.tenant_id
            or obj.generation != generation
            or obj.revision != revision
            or _busy(obj)
        ):
            return
        row = db.exec(
            select(IngestSessionFile).where(
                col(IngestSessionFile.tenant_id) == context.tenant_id,
                col(IngestSessionFile.bc_id) == obj.bc_id,
                col(IngestSessionFile.material_id) == obj.material_id,
                col(IngestSessionFile.current_generation) == generation,
            )
        ).one_or_none()
        if (
            row is None
            or (
                row.error_code == "user_cancelled"
                and obj.error_code == "multipart_collecting"
            )
            or obj.error_code not in RECOVERY_ERRORS
        ):
            return
        identity = IngestIdentity(
            generation=generation,
            upload_id=obj.s3_upload_id,
            operation_revision=revision,
        )
        session_id, material_id, phase = row.session_id, obj.material_id, obj.error_code
    if phase in {"multipart_creating", "multipart_create_unknown"}:
        resume_file(
            database_engine=database_engine,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            s3=s3,
        )
    else:
        complete_file(
            database_engine=database_engine,
            context=context,
            session_id=session_id,
            material_id=material_id,
            identity=identity,
            s3=s3,
        )
