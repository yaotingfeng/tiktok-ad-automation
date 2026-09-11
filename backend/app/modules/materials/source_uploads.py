"""Durable source upload and readback, one network call per dispatch.

MaterialAssetOperation owns the only send claim for this file/account across
source upload and target preparation. Never hold a DB transaction over network.
"""

from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import PurePath
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.sdk import sdk_client
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.access import (
    resolve_account_access,
    usable_grants,
)
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from . import sdk_assets as api
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
    ObjectUpload,
    UploadBatch,
)
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
    require_sdk_route,
    source_parent_route,
)
from .storage import OriginalFile, open_original

UPLOAD_HARD_LIMIT = 900
READ_HARD_LIMIT = 45
UPLOAD_CLAIM_SECONDS = 960
READ_CLAIM_SECONDS = 60
UNRESOLVED = ("pending", "sending", "result_unknown", "verifying", "confirmed_absent")


def _material(
    session: Session,
    context: TenantContext,
    material_id: UUID,
    *,
    action: str = "upload",
    require_stored: bool = True,
) -> MaterialFile:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )
    row = _locked_material(session, context, material_id)
    if require_stored and row.storage_state != "stored":
        raise DomainError("original_unavailable", "素材原文件尚未完整入库")
    return row


def _locked_material(
    session: Session, context: TenantContext, material_id: UUID
) -> MaterialFile:
    """Scoped bookkeeping lock; callers separately authorize every action.

    Lock before operation even for finalization: inserting an AccountMaterial
    takes a foreign-key KEY SHARE lock on this file.
    """
    row = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.id == material_id, MaterialFile.tenant_id == context.tenant_id
        )
        .with_for_update()
    ).first()
    if row is None:
        raise DomainError("material_not_found", "未找到当前租户素材")
    return row


def remote_name(material: MaterialFile) -> str:
    suffix = PurePath(material.file_name).suffix.lower()
    # Never pass path fragments/user names to the platform correlation name.
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mpeg", ".3gp"}:
        suffix = ".mp4"
    return f"{material.id}{suffix}"


def reserve_asset_operation(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    advertiser_id: str,
    path: str,
    action: str = "upload",
    route: FrozenTikTokRoute,
) -> MaterialAssetOperation:
    """Caller transaction owns the file row lock; shared with Task4 writers."""
    if action not in {"upload", "build"}:
        raise DomainError("invalid_account_action", "素材操作动作无效")
    material = _material(
        session, context, material_id, action=action, require_stored=action == "upload"
    )
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=material.bc_id,
        advertiser_id=advertiser_id,
        capability="upload" if action == "upload" else "build",
    )
    if path not in {"upload_original", "share_source"}:
        raise DomainError("invalid_asset_path", "素材准备路径无效")
    existing = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.tenant_id == context.tenant_id,
            MaterialAssetOperation.material_id == material_id,
            MaterialAssetOperation.advertiser_id == advertiser_id,
            col(MaterialAssetOperation.status).in_(UNRESOLVED),
        )
        .with_for_update()
    ).first()
    if existing:
        existing_route = load_material_route(
            existing.frozen_route, context=context, bc_id=material.bc_id
        )
        # 已存在源上传仅供目标观察，不能把同一远端身份改成另一次上传。
        if _attempt(session, existing.id) is None:
            require_same_route(existing_route, route)
        return existing
    operation = MaterialAssetOperation(
        tenant_id=context.tenant_id,
        bc_id=material.bc_id,
        material_id=material.id,
        advertiser_id=advertiser_id,
        path=path,
        frozen_route=route.model_dump(mode="json"),
        request_digest=sha256(
            f"{context.tenant_id}:{material.id}:{advertiser_id}:{path}".encode()
        ).hexdigest(),
    )
    session.add(operation)
    session.flush()
    return operation


def _queue(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    operation_id: UUID | None,
    kind: str,
    due: datetime | None = None,
    recovery_claim_id: UUID | None = None,
    object_id: UUID | None = None,
    generation: int | None = None,
) -> UUID:
    payload: dict[str, Any] = {"material_id": str(material_id)}
    if object_id is not None:
        payload.update(object_id=str(object_id), generation=generation)
    if operation_id:
        payload["operation_id"] = str(operation_id)
        operation = session.get(MaterialAssetOperation, operation_id)
        assert operation
        if recovery_claim_id:
            payload["claim_id"] = str(recovery_claim_id)
        else:
            payload["revision"] = operation.remote_response.get("revision", 0)
    dispatch_id = enqueue_after_commit(
        session,
        context=context,
        task_name=f"materials.{kind}_original",
        task_key=f"material:{material_id}:{uuid4()}",
        payload=payload,
    )
    if object_id is not None:
        from .ingest_models import IngestSessionFile

        ingest_file = session.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.tenant_id == context.tenant_id,
                IngestSessionFile.material_id == material_id,
                IngestSessionFile.current_generation == generation,
            )
        ).one()
        ingest_file.dispatch_id = dispatch_id
    if due:
        dispatch = session.get(PendingDispatch, dispatch_id)
        assert dispatch
        dispatch.available_at = due
    return dispatch_id


def _attempt(session: Session, operation_id: UUID) -> MaterialUploadAttempt | None:
    return session.exec(
        select(MaterialUploadAttempt)
        .where(MaterialUploadAttempt.operation_id == operation_id)
        .order_by(
            col(MaterialUploadAttempt.created_at).desc(),
            col(MaterialUploadAttempt.id).desc(),
        )
    ).first()


def _object_error(session: Session, material: MaterialFile, code: str | None) -> None:
    row = session.exec(
        select(ObjectUpload).where(
            ObjectUpload.tenant_id == material.tenant_id,
            ObjectUpload.material_id == material.id,
        )
    ).first()
    if row:
        row.error_code = code
        row.status = "blocked" if code else "stored"
        _refresh_batch(session, material.tenant_id, material.id)


def _record_unsent_denial(
    session: Session, *, context: TenantContext, material_id: UUID, code: str
) -> bool:
    """Internal task bookkeeping only; never grants the denied upload action.

    The original batch actor and exact tenant/file must match. An existing
    source attempt owns its own recovery and must not become a fresh retry.
    """
    material = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.id == material_id,
        )
        .with_for_update()
    ).first()
    if material is None or material.storage_state != "stored":
        return False
    upload = session.exec(
        select(ObjectUpload).where(
            ObjectUpload.tenant_id == context.tenant_id,
            ObjectUpload.material_id == material_id,
        )
    ).first()
    batch = session.get(UploadBatch, upload.batch_id) if upload else None
    if (
        batch is None
        or batch.tenant_id != context.tenant_id
        or batch.actor_id != context.actor_id
    ):
        return False
    started = session.exec(
        select(MaterialUploadAttempt.id).where(
            MaterialUploadAttempt.tenant_id == context.tenant_id,
            MaterialUploadAttempt.material_id == material_id,
        )
    ).first()
    if started is not None:
        return False
    _object_error(session, material, code)
    return True


def _source_access(
    session: Session,
    *,
    context: TenantContext,
    work: dict[str, Any],
    upload: bool = True,
) -> AccountAccess:
    route = load_material_route(
        work.get("frozen_route"),
        context=context,
        bc_id=work["bc_id"],
        connection_id=UUID(str(work["connection_id"])),
    )
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=work["bc_id"],
        advertiser_id=work["advertiser_id"],
        capability="upload" if upload else "read",
    )
    require_sdk_route(route)
    return resolve_account_access(
        session,
        context=context,
        bc_id=work["bc_id"],
        advertiser_id=work["advertiser_id"],
        action="upload" if upload else "read",
        connection_id=route.connection_id,
    )


def _has_verified_mapping(
    session: Session, context: TenantContext, material_id: UUID, advertiser_id: str
) -> bool:
    asset = session.exec(
        select(AccountMaterial).where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.material_id == material_id,
            AccountMaterial.advertiser_id == advertiser_id,
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
        )
    ).first()
    return bool(asset and asset.video_id.strip())


def _save_attempt_evidence(
    attempt: MaterialUploadAttempt,
    operation: MaterialAssetOperation,
    upload_receipt: dict[str, str] | None = None,
) -> None:
    original = {
        key: value
        for key, value in attempt.remote_response.items()
        if key in {"upload_video_id", "upload_mid"}
    }
    if upload_receipt:
        original["upload_video_id"] = upload_receipt["video_id"]
        if upload_receipt.get("mid"):
            original["upload_mid"] = upload_receipt["mid"]
    attempt.remote_response = {**original, **operation.remote_response}


def _refresh_batch(session: Session, tenant_id: UUID, material_id: UUID) -> None:
    upload = session.exec(
        select(ObjectUpload).where(
            ObjectUpload.tenant_id == tenant_id,
            ObjectUpload.material_id == material_id,
        )
    ).first()
    if upload:
        from .uploads import refresh_upload_batch

        refresh_upload_batch(session, tenant_id=tenant_id, batch_id=upload.batch_id)


def _assigned_source(
    session: Session, *, context: TenantContext, material: MaterialFile
) -> tuple[AccountAccess, FrozenTikTokRoute]:
    route = source_parent_route(
        session,
        context=context,
        material_id=material.id,
        bc_id=material.bc_id,
        generation=material.current_object_generation,
    )
    require_sdk_route(route)
    grant = session.exec(
        usable_grants(
            tenant_id=context.tenant_id, bc_id=material.bc_id, action="upload"
        )
        .where(BCAccountAccess.connection_id == route.connection_id)
        .order_by(BCAccountAccess.advertiser_id)
        .limit(1)
    ).first()
    if grant is None:
        raise DomainError("no_upload_account", "原上传连接没有可上传的授权账户")
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=material.bc_id,
        advertiser_id=grant.advertiser_id,
        capability="upload",
    )
    return resolve_account_access(
        session,
        context=context,
        bc_id=material.bc_id,
        advertiser_id=grant.advertiser_id,
        action="upload",
        connection_id=route.connection_id,
    ), route


def request_source_retry(
    session: Session, *, context: TenantContext, material_id: UUID
) -> UUID:
    """Explicit user retry of a provably unsent failure; caller commits the outbox.

    An unknown/verifying send never changes account or becomes a blind retry.
    Each new attempt records its actual selected connection/account permanently.
    """
    material = _material(session, context, material_id, require_stored=False)
    if material.current_object_generation is not None:
        from .source_url_uploads import request_url_retry

        return request_url_retry(session, context=context, material_id=material_id)
    if material.storage_state != "stored":
        raise DomainError("original_unavailable", "素材原文件尚未完整入库")
    previous = session.exec(
        select(MaterialUploadAttempt)
        .where(
            MaterialUploadAttempt.tenant_id == context.tenant_id,
            MaterialUploadAttempt.material_id == material_id,
        )
        .order_by(
            col(MaterialUploadAttempt.created_at).desc(),
            col(MaterialUploadAttempt.id).desc(),
        )
    ).first()
    if previous is None:
        # No account was available before any platform attempt was established.
        access, route = _assigned_source(session, context=context, material=material)
    else:
        previous_op = _locked_operation(session, context, previous.operation_id)
        if (
            previous_op.status != "failed"
            or previous.status not in {"failed", "blocked"}
            or previous_op.attempt_token
            or previous_op.remote_response.get("video_id")
            or previous_op.remote_response.get("mid")
        ):
            raise DomainError(
                "material_retry_not_allowed", "仅允许重试尚未发送的平台失败项"
            )
        access, route = _assigned_source(session, context=context, material=material)
    if material.byte_size > settings.MATERIAL_SDK_MAX_UPLOAD_BYTES:
        raise DomainError(
            "sdk_upload_capacity_exceeded", "原文件超过当前平台上传内存容量边界"
        )
    if _has_verified_mapping(session, context, material_id, access.advertiser_id):
        raise DomainError("material_retry_not_allowed", "该账户素材已经核实可用")
    operation = reserve_asset_operation(
        session,
        context=context,
        material_id=material_id,
        advertiser_id=access.advertiser_id,
        path="upload_original",
        route=route,
    )
    if (
        _attempt(session, operation.id) is not None
        or operation.attempt_token
        or operation.status != "pending"
        or operation.path != "upload_original"
    ):
        raise DomainError("material_retry_not_allowed", "该账户素材已有未核实操作")
    session.add(
        MaterialUploadAttempt(
            tenant_id=context.tenant_id,
            bc_id=material.bc_id,
            material_id=material_id,
            advertiser_id=access.advertiser_id,
            connection_id=access.connection_id,
            operation_id=operation.id,
            request_digest=operation.request_digest,
        )
    )
    session.flush()
    _object_error(session, material, None)
    _refresh_batch(session, context.tenant_id, material_id)
    return _queue(
        session,
        context=context,
        material_id=material_id,
        operation_id=operation.id,
        kind="upload",
    )


def _locked_operation(
    session: Session, context: TenantContext, operation_id: UUID
) -> MaterialAssetOperation:
    operation = session.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.id == operation_id,
            MaterialAssetOperation.tenant_id == context.tenant_id,
        )
        .with_for_update()
    ).one()
    return operation


def run_source_upload(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    material_id: UUID,
    kind: str = "upload",
    operation_id: UUID | None = None,
    s3: Any = None,
    recovery_claim_id: UUID | None = None,
    revision: int | None = None,
    object_id: UUID | None = None,
    generation: int | None = None,
) -> None:
    """Testable worker body; production wrapper MUST enforce the hard process limit."""
    # Repair dispatches may reference a known operation; its durable metadata
    # supplies the exact generation. An initial new-path message must supply it.
    if object_id is None and operation_id is not None:
        with Session(database_engine) as lookup:
            op = lookup.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.id == operation_id,
                    MaterialAssetOperation.tenant_id == context.tenant_id,
                    MaterialAssetOperation.material_id == material_id,
                )
            ).first()
            if op is not None and op.remote_response.get("object_id"):
                object_id = UUID(op.remote_response["object_id"])
                generation = op.remote_response.get("generation")
    if object_id is not None or generation is not None:
        if object_id is None or type(generation) is not int or generation < 1:
            raise DomainError("invalid_asset_task", "原件任务缺少精确代次")
        from .source_url_uploads import run_url_source_upload

        return run_url_source_upload(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
            kind=kind,
            operation_id=operation_id,
            s3=s3,
            recovery_claim_id=recovery_claim_id,
            revision=revision,
        )
    with Session(database_engine) as lookup:
        current = lookup.exec(
            select(MaterialFile.current_object_generation).where(
                MaterialFile.id == material_id,
                MaterialFile.tenant_id == context.tenant_id,
            )
        ).first()
        if current is not None:
            raise DomainError("invalid_asset_task", "代次素材不能回退旧文件上传路径")
    if kind not in {"upload", "verify"}:
        raise DomainError("invalid_asset_task", "素材工作任务无效")
    claim = uuid4()
    hard_limit = UPLOAD_HARD_LIMIT if kind == "upload" else READ_HARD_LIMIT
    claim_seconds = UPLOAD_CLAIM_SECONDS if kind == "upload" else READ_CLAIM_SECONDS
    deadline = datetime.now(UTC) + timedelta(seconds=hard_limit - 5)
    with Session(database_engine) as session, session.begin():
        try:
            material = _material(
                session,
                context,
                material_id,
                action="upload" if kind == "upload" else "read",
                require_stored=False,
            )
        except DomainError as error:
            if (
                kind == "upload"
                and operation_id is None
                and error.code in {"action_forbidden", "tenant_forbidden"}
                and _record_unsent_denial(
                    session, context=context, material_id=material_id, code=error.code
                )
            ):
                return
            raise
        if operation_id:
            operation = session.exec(
                select(MaterialAssetOperation)
                .where(
                    MaterialAssetOperation.id == operation_id,
                    MaterialAssetOperation.tenant_id == context.tenant_id,
                    MaterialAssetOperation.material_id == material_id,
                )
                .with_for_update()
            ).first()
            if operation is None:
                raise DomainError("material_operation_not_found", "未找到素材操作")
            attempt = _attempt(session, operation.id)
        else:
            if kind != "upload":
                raise DomainError("invalid_asset_task", "核实任务缺少操作标识")
            attempt = session.exec(
                select(MaterialUploadAttempt)
                .where(
                    MaterialUploadAttempt.tenant_id == context.tenant_id,
                    MaterialUploadAttempt.material_id == material_id,
                )
                .order_by(
                    col(MaterialUploadAttempt.created_at).desc(),
                    col(MaterialUploadAttempt.id).desc(),
                )
            ).first()
            operation = (
                _locked_operation(session, context, attempt.operation_id)
                if attempt
                else None
            )
            if operation is None:
                try:
                    access, route = _assigned_source(
                        session, context=context, material=material
                    )
                except DomainError as error:
                    _object_error(session, material, error.code)
                    return
                if _has_verified_mapping(
                    session, context, material_id, access.advertiser_id
                ):
                    return
                operation = reserve_asset_operation(
                    session,
                    context=context,
                    material_id=material_id,
                    advertiser_id=access.advertiser_id,
                    path="upload_original",
                    route=route,
                )
                # An existing distribution owns this unresolved operation. Source
                # delivery must never establish a second send authority.
                attempt = _attempt(session, operation.id)
                if attempt is None and (
                    operation.path != "upload_original"
                    or operation.status != "pending"
                    or operation.attempt_token
                ):
                    return
                if attempt is None:
                    attempt = MaterialUploadAttempt(
                        tenant_id=context.tenant_id,
                        bc_id=material.bc_id,
                        material_id=material_id,
                        advertiser_id=access.advertiser_id,
                        connection_id=access.connection_id,
                        operation_id=operation.id,
                        request_digest=operation.request_digest,
                    )
                    session.add(attempt)
                    session.flush()
        if (
            not attempt
            or operation.path != "upload_original"
            or operation.status not in UNRESOLVED
        ):
            return
        if recovery_claim_id and operation.attempt_token != recovery_claim_id:
            return
        if revision is not None and revision != operation.remote_response.get(
            "revision", 0
        ):
            return
        now = datetime.now(UTC)
        if operation.claimed_until and operation.claimed_until > now:
            return
        expired_send = operation.status == "sending"
        if expired_send:
            operation.status = "result_unknown"
            attempt.status = "result_unknown"
        expected_kind = (
            "upload"
            if operation.status in {"pending", "confirmed_absent"}
            else "verify"
        )
        if expected_kind != kind:
            if expired_send:
                operation.attempt_token, operation.claimed_until = None, None
                _queue(
                    session,
                    context=context,
                    material_id=material_id,
                    operation_id=operation.id,
                    kind=expected_kind,
                )
            return
        if (
            kind == "upload"
            and material.byte_size > settings.MATERIAL_SDK_MAX_UPLOAD_BYTES
        ):
            operation.status, attempt.status = "failed", "blocked"
            operation.remote_response = attempt.remote_response = {
                "error_code": "sdk_upload_capacity_exceeded"
            }
            _object_error(session, material, "sdk_upload_capacity_exceeded")
            return
        previous_status = operation.status
        current_revision = operation.remote_response.get("revision", 0) + 1
        operation.remote_response = {
            **operation.remote_response,
            "revision": current_revision,
        }
        operation.attempt_token = claim
        operation.claimed_until = now + timedelta(seconds=claim_seconds)
        operation_id = operation.id
        # Recovery is durable BEFORE S3, Redis, credentials, or SDK invocation.
        _queue(
            session,
            context=context,
            material_id=material_id,
            operation_id=operation.id,
            kind=kind,
            due=operation.claimed_until,
            recovery_claim_id=claim,
        )
        _refresh_batch(session, context.tenant_id, material_id)
        content_md5 = material.video_md5 or ""
        work: dict[str, Any] = {
            "frozen_route": operation.frozen_route,
            "bc_id": material.bc_id,
            "advertiser_id": operation.advertiser_id,
            "connection_id": attempt.connection_id,
            "remote_name": remote_name(material),
            **operation.remote_response,
        }
    sent = False
    evidence: dict[str, str] | tuple[list[dict[str, str]], bool] | None = None
    original_scope: AbstractContextManager[OriginalFile | None]
    try:
        # The original adapter completes its DB work before streaming to disk.
        if kind == "upload":
            original_scope = open_original(
                database_engine=database_engine,
                context=context,
                bc_id=work["bc_id"],
                material_id=material_id,
                action="upload",
                s3=s3,
                deadline=deadline,
            )
        else:
            if len(content_md5) != 32:
                raise DomainError("material_digest_missing", "素材缺少可核实内容摘要")
            original_scope = nullcontext(None)
        with original_scope as original:
            endpoint = (
                api.UPLOAD_ENDPOINT
                if kind == "upload"
                else api.INFO_ENDPOINT
                if work.get("video_id")
                else api.SEARCH_ENDPOINT
            )
            policy = admission_policy(endpoint)
            if policy.lease_ms <= (hard_limit + 5) * 1000:
                raise DomainError(
                    "admission_policy_invalid",
                    "素材调用租约必须长于工作进程硬限及清理余量",
                )
            if (
                kind == "upload"
                and policy.endpoint_max_inflight
                > settings.MATERIAL_SDK_UPLOAD_MAX_INFLIGHT
            ):
                raise DomainError(
                    "admission_policy_invalid", "素材上传并发超过配置的内存容量边界"
                )
            with api.admitted_asset_call(
                redis_client,
                context=context,
                endpoint=endpoint,
                advertiser_id=work["advertiser_id"],
                policy=policy,
            ):
                with Session(database_engine) as session:
                    # Every phase that updates file metadata locks file before
                    # operation, matching initial claim and duplicate delivery.
                    sending_material = _material(
                        session,
                        context,
                        material_id,
                        action="upload" if kind == "upload" else "read",
                        require_stored=False,
                    )
                    operation = _locked_operation(session, context, operation_id)
                    if operation.attempt_token != claim:
                        return
                    access = _source_access(
                        session, context=context, work=work, upload=kind == "upload"
                    )
                    with sdk_client(
                        session, context=context, connection_id=access.connection_id
                    ) as client:
                        if datetime.now(UTC) >= deadline:
                            raise DomainError(
                                "material_deadline",
                                "素材处理已到达本次期限",
                                retryable=True,
                            )
                        if original:
                            sending_material.sha256, sending_material.video_md5 = (
                                original.sha256,
                                original.md5,
                            )
                            content_md5 = original.md5
                            operation.status = "sending"
                            current_attempt = _attempt(session, operation.id)
                            assert current_attempt
                            operation.request_digest = sha256(
                                f"{work['advertiser_id']}:{work['remote_name']}:{original.sha256}:{original.md5}".encode()
                            ).hexdigest()
                            current_attempt.request_digest = operation.request_digest
                            current_attempt.status = "uploading"
                            _refresh_batch(session, context.tenant_id, material_id)
                        session.commit()
                        session.close()  # no transaction/row lock across SDK I/O
                        sent = True
                        if kind == "upload":
                            assert original
                            evidence = api.parse_upload(
                                api.upload_video(
                                    client,
                                    advertiser_id=work["advertiser_id"],
                                    local_path=original.path,
                                    remote_name=work["remote_name"],
                                    md5=original.md5,
                                )
                            )
                        elif work.get("video_id"):
                            evidence = api.verified_video(
                                api.read_video(
                                    client,
                                    advertiser_id=work["advertiser_id"],
                                    video_id=work["video_id"],
                                ),
                                md5=content_md5,
                            )
                        else:
                            evidence = api.search_page(
                                api.search_videos(
                                    client,
                                    advertiser_id=work["advertiser_id"],
                                    page=work.get("search_page", 1),
                                ),
                                page=work.get("search_page", 1),
                                remote_name=work["remote_name"],
                                md5=content_md5,
                            )
        with Session(database_engine) as session, session.begin():
            _locked_material(session, context, material_id)
            operation = _locked_operation(session, context, operation_id)
            if operation.attempt_token != claim:
                return
            attempt = _attempt(session, operation.id)
            assert attempt
            if kind == "upload":
                assert isinstance(evidence, dict)
                operation.remote_response = {**evidence}
                operation.status, attempt.status = "verifying", "verifying"
            elif work.get("video_id") and evidence:
                assert isinstance(evidence, dict)
                # Fresh authority before readiness. Revocation keeps evidence but
                # cannot turn stale permission into an available mapping.
                access = _source_access(
                    session, context=context, work=work, upload=kind == "upload"
                )
                asset = session.exec(
                    select(AccountMaterial).where(
                        AccountMaterial.tenant_id == context.tenant_id,
                        AccountMaterial.material_id == material_id,
                        AccountMaterial.advertiser_id == work["advertiser_id"],
                    )
                ).first()
                if not asset:
                    asset = AccountMaterial(
                        tenant_id=context.tenant_id,
                        bc_id=work["bc_id"],
                        material_id=material_id,
                        advertiser_id=work["advertiser_id"],
                        connection_id=access.connection_id,
                        video_id=evidence["video_id"],
                    )
                    session.add(asset)
                asset.connection_id, asset.video_id = (
                    access.connection_id,
                    evidence["video_id"],
                )
                asset.mid, asset.status, asset.verified_at = (
                    evidence.get("mid"),
                    "available",
                    datetime.now(UTC),
                )
                operation.remote_response = {**evidence}
                operation.status, attempt.status = "succeeded", "available"
            elif not work.get("video_id"):
                assert isinstance(evidence, tuple)
                matches, last = evidence
                candidates = {
                    item["video_id"]: item for item in work.get("candidates", [])
                }
                candidates.update({item["video_id"]: item for item in matches})
                if len(candidates) > 1:
                    operation.remote_response = {
                        "error_code": "material_reconciliation_ambiguous"
                    }
                    operation.status, attempt.status = (
                        "result_unknown",
                        "result_unknown",
                    )
                elif last and candidates:
                    operation.remote_response = next(iter(candidates.values()))
                    operation.status, attempt.status = "verifying", "verifying"
                else:
                    operation.remote_response = {
                        "search_page": 1 if last else work.get("search_page", 1) + 1,
                        "candidates": [] if last else list(candidates.values()),
                        "error_code": "material_result_pending",
                    }
                    operation.status, attempt.status = (
                        "result_unknown",
                        "result_unknown",
                    )
            else:
                operation.status, attempt.status = "verifying", "verifying"
                operation.remote_response = {
                    **operation.remote_response,
                    "error_code": "material_result_pending",
                }
            operation.remote_response = {
                **operation.remote_response,
                "revision": current_revision,
            }
            _save_attempt_evidence(
                attempt,
                operation,
                evidence if kind == "upload" and isinstance(evidence, dict) else None,
            )
            _refresh_batch(session, context.tenant_id, material_id)
            operation.attempt_token, operation.claimed_until = None, None
            if operation.status != "succeeded":
                _queue(
                    session,
                    context=context,
                    material_id=material_id,
                    operation_id=operation_id,
                    kind="verify",
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
    except Exception as error:
        # Never persist/raise raw SDK exceptions (tokens, URLs and file paths).
        with Session(database_engine) as session, session.begin():
            _locked_material(session, context, material_id)
            operation = _locked_operation(session, context, operation_id)
            if operation.attempt_token != claim:
                return
            attempt = _attempt(session, operation_id)
            assert attempt
            code = (
                error.code
                if isinstance(error, DomainError)
                else "material_response_unknown"
            )
            deferred = (
                isinstance(error, api.SdkAdmissionDeferred)
                or code == "admission_unavailable"
            )
            operation.status = (
                previous_status
                if deferred
                else "result_unknown"
                if sent or kind == "verify"
                else "failed"
            )
            attempt.status = (
                "pending"
                if deferred and kind == "upload"
                else "result_unknown"
                if operation.status == "result_unknown"
                else "verifying"
                if deferred
                else "blocked"
            )
            operation.remote_response = {
                **operation.remote_response,
                "error_code": code,
            }
            operation.remote_response = {
                **operation.remote_response,
                "revision": current_revision,
            }
            _save_attempt_evidence(attempt, operation)
            _refresh_batch(session, context.tenant_id, material_id)
            operation.attempt_token, operation.claimed_until = None, None
            if operation.status != "failed":
                delay = (
                    max(1, error.retry_after_ms) / 1000
                    if isinstance(error, api.SdkAdmissionDeferred)
                    else 60
                )
                _queue(
                    session,
                    context=context,
                    material_id=material_id,
                    operation_id=operation_id,
                    kind=kind if deferred else "verify",
                    due=datetime.now(UTC) + timedelta(seconds=delay),
                )
