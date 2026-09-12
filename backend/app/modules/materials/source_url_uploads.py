"""One bounded channel request per generation-fenced source dispatch.

No original download; verified channel policy precedes signing and arming.
Only explicit NOT_SENT releases this attempt's temporary signed use.
Known receipts survive later client cleanup and permission changes. UNKNOWN
retains the same source/connection/object/operation and its remote-use claim.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.jobs.admission import admission_policy
from app.modules.tenants.permissions import require_tenant

from . import sdk_assets as api
from .channel_policy import require_url_upload
from .ingest_models import (
    IngestSession,
    IngestSessionFile,
    TemporaryMaterialObject,
    record_milestone,
    transition_ingest_file,
)
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
)
from .routes import load_material_route, source_parent_route
from .source_selection import claim_source_account, release_source_account, source_file
from .source_uploads import (
    READ_CLAIM_SECONDS,
    READ_HARD_LIMIT,
    UPLOAD_CLAIM_SECONDS,
    UPLOAD_HARD_LIMIT,
    _attempt,
    _locked_material,
    _locked_operation,
    _queue,
    _save_attempt_evidence,
    _source_access,
)

MAX_SEARCH_PAGES = 100

MAX_SEARCH_ROWS = MAX_SEARCH_PAGES * 100


@dataclass(frozen=True)
class _SearchPage:
    page: int
    total_pages: int
    total_number: int
    ids: tuple[str, ...]
    matches: list[dict[str, str]]


def _search_progress(work: dict[str, Any], page: _SearchPage) -> dict[str, Any]:
    # gateway每页重建，adapter内存中的跨页证明不能代替持久进度。
    # 只保存有界ID摘要，不保存无关账户字段或远端原文。
    reset: dict[str, Any] = {
        "search_page": 1,
        "search_total": None,
        "search_count": None,
        "search_seen": [],
        "candidates": [],
        "error_code": "material_result_pending",
    }
    previous = work.get("search_seen", [])
    valid_previous = (
        type(previous) is list
        and len(previous) <= MAX_SEARCH_ROWS
        and all(
            type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in previous
        )
        and len(set(previous)) == len(previous)
    )
    if not valid_previous:
        return reset
    previous_ids = cast(list[str], previous)
    seen: set[str] = set(previous_ids) if page.page > 1 else set()
    ids = {sha256(identity.encode()).hexdigest() for identity in page.ids}
    if (
        page.page != work.get("search_page", 1)
        or not 1 <= page.page <= MAX_SEARCH_PAGES
        or not 0 <= page.total_pages <= MAX_SEARCH_PAGES
        or not 0 <= page.total_number <= MAX_SEARCH_ROWS
        or len(ids) != len(page.ids)
        or seen & ids
        or (
            page.page > 1
            and (
                work.get("search_total") != page.total_pages
                or work.get("search_count") != page.total_number
                or len(seen) != (page.page - 1) * 100
            )
        )
    ):
        return reset
    seen |= ids
    last = page.page >= page.total_pages
    if (
        len(seen) > MAX_SEARCH_ROWS
        or (last and len(seen) != page.total_number)
        or (not last and len(seen) != page.page * 100)
    ):
        return reset
    candidates = {
        item["video_id"]: item
        for item in (work.get("candidates", []) if page.page > 1 else [])
    }
    candidates.update({item["video_id"]: item for item in page.matches})
    if len(candidates) > 1:
        return {
            **reset,
            "candidates": list(candidates.values())[:2],
            "error_code": "material_reconciliation_ambiguous",
        }
    if last:
        return {**reset, **(next(iter(candidates.values())) if candidates else {})}
    return {
        "search_page": page.page + 1,
        "search_total": page.total_pages,
        "search_count": page.total_number,
        "search_seen": sorted(seen),
        "candidates": list(candidates.values()),
        "error_code": "material_result_pending",
    }


def sign_ingest_url(**kwargs: Any) -> str:
    # Task 2 owns signing + the durable OriginalUse. Import only when the new
    # path executes so the compatibility FILE path does not require R2 setup.
    from .object_validation import sign_ingest_url as sign

    return sign(**kwargs)


def _records(
    db: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    object_id: UUID,
    generation: int,
) -> tuple[MaterialFile, TemporaryMaterialObject, IngestSessionFile]:
    material = _locked_material(db, context, material_id)
    obj = db.exec(
        select(TemporaryMaterialObject)
        .where(
            TemporaryMaterialObject.id == object_id,
            TemporaryMaterialObject.tenant_id == context.tenant_id,
            TemporaryMaterialObject.bc_id == material.bc_id,
            TemporaryMaterialObject.material_id == material_id,
            TemporaryMaterialObject.generation == generation,
        )
        .with_for_update()
    ).first()
    if obj is None:
        raise DomainError("object_identity_unverified", "原件代次身份无法核实")
    row = source_file(
        db, context=context, bc_id=material.bc_id, material_id=material_id
    )
    parent = db.get(IngestSession, row.session_id)
    if parent is None or parent.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "导入操作人不匹配")
    return material, obj, row


def _proof(
    material: MaterialFile,
    obj: TemporaryMaterialObject,
    row: IngestSessionFile,
    *,
    upload: bool,
) -> None:
    if (
        material.current_object_generation != obj.generation
        or row.current_generation != obj.generation
    ):
        raise DomainError("object_generation_changed", "原件代次已改变")
    if (
        not obj.digest_verified_at
        or not material.digest_verified_at
        or not re.fullmatch(r"[0-9a-f]{32}", obj.video_md5 or "")
        or not re.fullmatch(r"[0-9a-f]{64}", obj.sha256 or "")
        or (material.video_md5, material.sha256) != (obj.video_md5, obj.sha256)
        or obj.actual_bytes != obj.expected_bytes
        or obj.expected_bytes != material.byte_size
    ):
        raise DomainError("material_digest_missing", "素材缺少可信的整文件校验结果")
    if upload:
        if not getattr(settings, "MATERIAL_INGEST_ENABLED", False):
            raise DomainError("material_ingest_disabled", "素材入库暂未启用")
        if obj.status != "verified":
            raise DomainError("original_unavailable", "原件尚未完成可信校验或正在清理")
        if obj.expected_bytes > settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
            raise DomainError(
                "url_upload_capacity_exceeded", "原件超过当前 URL 入库工程容量限制"
            )


def _state(
    db: Session,
    context: TenantContext,
    row: IngestSessionFile,
    status: str,
    code: str | None = None,
) -> None:
    transition_ingest_file(
        db,
        tenant_id=context.tenant_id,
        file_id=row.id,
        expected_revision=row.revision,
        status=status,
        error_code=code,
    )
    # Same status can still carry a new application-owned explanation.
    row.error_code = code


def _new_operation(
    db: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    obj: TemporaryMaterialObject,
    source_advertiser_id: str | None = None,
) -> MaterialAssetOperation:
    route = source_parent_route(
        db,
        context=context,
        material_id=material.id,
        bc_id=material.bc_id,
        generation=obj.generation,
    )
    if route.channel == "OFFICIAL_MCP":
        require_url_upload(
            material_upload_policy(
                channel=route.channel,
                adapter_contract_revision=route.adapter_contract_revision,
            ),
            byte_size=obj.expected_bytes,
        )
    access = claim_source_account(
        db,
        context=context,
        bc_id=material.bc_id,
        material_id=material.id,
        # A new operation always charges its own slot. A persisted file tuple
        # can belong to a finished, released operation from an older generation.
        reselect=True,
        route=route,
        advertiser_id=source_advertiser_id,
    )
    conflicting = db.exec(
        select(MaterialAssetOperation.id).where(
            MaterialAssetOperation.tenant_id == context.tenant_id,
            MaterialAssetOperation.material_id == material.id,
            MaterialAssetOperation.advertiser_id == access.advertiser_id,
            col(MaterialAssetOperation.status).in_(
                (
                    "pending",
                    "sending",
                    "result_unknown",
                    "verifying",
                    "confirmed_absent",
                )
            ),
        )
    ).first()
    if conflicting:
        raise DomainError("material_operation_conflict", "该目标账户已有未核实素材操作")
    stem = material.file_name.rsplit(".", 1)[0]
    stem = re.sub(r"[\x00-\x1f\x7f/\\]", "_", stem).strip() or "video"
    # Keep a recognizable original name with a stable, persisted correlation
    # suffix. Bound UTF-8 bytes as well as characters for the remote field.
    suffix = f"-{material.id}-{obj.generation}-{uuid4().hex[:12]}.mp4"
    # TikTok 限制的是完整文件名；先给关联后缀留空间，避免正常原名加后缀就超长。
    stem = stem.encode("utf-8")[: 100 - len(suffix)].decode("utf-8", errors="ignore")
    name = f"{stem}{suffix}"
    digest = sha256(
        f"{context.tenant_id}:{material.id}:{obj.id}:{obj.generation}:{access.advertiser_id}:{name}:{obj.sha256}:{obj.video_md5}".encode()
    ).hexdigest()
    operation = MaterialAssetOperation(
        tenant_id=context.tenant_id,
        bc_id=material.bc_id,
        material_id=material.id,
        advertiser_id=access.advertiser_id,
        path="upload_original",
        request_digest=digest,
        frozen_route=route.model_dump(mode="json"),
        remote_response={
            "object_id": str(obj.id),
            "generation": obj.generation,
            "connection_id": str(access.connection_id),
            "remote_name": name,
            "source_slot_held": True,
            "send_armed": False,
            "revision": 0,
        },
    )
    db.add(operation)
    db.flush()
    db.add(
        MaterialUploadAttempt(
            tenant_id=context.tenant_id,
            bc_id=material.bc_id,
            material_id=material.id,
            advertiser_id=access.advertiser_id,
            connection_id=access.connection_id,
            operation_id=operation.id,
            request_digest=digest,
        )
    )
    db.flush()
    return operation


def request_url_retry(
    db: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    source_advertiser_id: str | None = None,
) -> UUID:
    require_tenant(
        db, tenant_id=context.tenant_id, actor_id=context.actor_id, action="upload"
    )
    material = _locked_material(db, context, material_id)
    obj = db.exec(
        select(TemporaryMaterialObject).where(
            TemporaryMaterialObject.tenant_id == context.tenant_id,
            TemporaryMaterialObject.material_id == material_id,
            TemporaryMaterialObject.generation == material.current_object_generation,
        )
    ).first()
    if obj is None:
        raise DomainError("object_identity_unverified", "原件身份无法核实")
    material, obj, row = _records(
        db,
        context=context,
        material_id=material_id,
        object_id=obj.id,
        generation=obj.generation,
    )
    _proof(material, obj, row, upload=True)
    operation = _new_operation(
        db,
        context=context,
        material=material,
        obj=obj,
        source_advertiser_id=source_advertiser_id,
    )
    _state(db, context, row, "stored")
    return _queue(
        db,
        context=context,
        material_id=material_id,
        operation_id=operation.id,
        kind="upload",
        object_id=obj.id,
        generation=obj.generation,
    )


def _release_use(
    db: Session,
    *,
    context: TenantContext,
    obj: TemporaryMaterialObject,
    operation: MaterialAssetOperation,
) -> None:
    # Caller holds the exact object and operation; completion evidence is already
    # checked, including the current actor/BC or a proven-unsent owner failure.
    from .object_uses import release_object_uses

    if obj.tenant_id != context.tenant_id:
        raise DomainError("tenant_forbidden", "原件不属于当前租户")
    release_object_uses(
        db, object_id=obj.id, purpose="ingest", operation_id=operation.id
    )


def _receipt(
    database_engine: Any,
    *,
    context: TenantContext,
    material_id: UUID,
    object_id: UUID,
    generation: int,
    operation_id: UUID,
    claim: UUID,
    evidence: dict[str, str],
) -> None:
    with Session(database_engine) as db, db.begin():
        _records(
            db,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
        )
        operation = _locked_operation(db, context, operation_id)
        attempt = _attempt(db, operation_id)
        assert attempt
        existing = operation.remote_response.get("video_id")
        if existing and existing != evidence["video_id"]:
            operation.remote_response = {
                **operation.remote_response,
                "error_code": "material_reconciliation_ambiguous",
                "conflicting_video_id": evidence["video_id"],
            }
            # Keep both actual IDs as recovery evidence, bounded to two.
            attempt.remote_response = {
                **attempt.remote_response,
                "conflicting_video_id": evidence["video_id"],
            }
            return
        operation.remote_response = {**operation.remote_response, **evidence}
        if operation.attempt_token == claim:
            operation.status, attempt.status = "verifying", "verifying"
        _save_attempt_evidence(attempt, operation, evidence)


def _finish(
    database_engine: Any,
    *,
    context: TenantContext,
    material_id: UUID,
    object_id: UUID,
    generation: int,
    operation_id: UUID,
    claim: UUID,
    work: dict[str, Any],
    kind: str,
    evidence: Any,
) -> None:
    with Session(database_engine) as db, db.begin():
        material, obj, row = _records(
            db,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
        )
        operation = _locked_operation(db, context, operation_id)
        if operation.attempt_token != claim:
            return
        attempt = _attempt(db, operation_id)
        assert attempt
        if kind == "verify" and work.get("video_id") and evidence:
            _proof(material, obj, row, upload=False)
            access = _source_access(
                db, context=context, work=work, upload=kind == "upload"
            )
            if (
                operation.remote_response.get("conflicting_video_id")
                or operation.remote_response.get("video_id") != evidence["video_id"]
            ):
                raise DomainError(
                    "material_reconciliation_ambiguous", "来源回执存在歧义"
                )
            asset = db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.tenant_id == context.tenant_id,
                    AccountMaterial.material_id == material_id,
                    AccountMaterial.advertiser_id == operation.advertiser_id,
                )
            ).first()
            if asset is None:
                asset = AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id=material.bc_id,
                    material_id=material_id,
                    advertiser_id=operation.advertiser_id,
                    connection_id=access.connection_id,
                    video_id=evidence["video_id"],
                )
                db.add(asset)
            asset.connection_id, asset.video_id, asset.mid = (
                access.connection_id,
                evidence["video_id"],
                evidence.get("mid"),
            )
            asset.status, asset.verified_at = "available", datetime.now(UTC)
            operation.status, attempt.status = "succeeded", "available"
            operation.remote_response = {**operation.remote_response, **evidence}
            db.flush()
            release_source_account(db, context=context, operation=operation)
            _release_use(db, context=context, obj=obj, operation=operation)
            _state(db, context, row, "available")
            record_milestone(
                db,
                tenant_id=context.tenant_id,
                bc_id=material.bc_id,
                session_id=row.session_id,
                material_id=material_id,
                milestone="ready",
            )
            from .cleanup import schedule_cleanup

            schedule_cleanup(db, object_id=obj.id, source_receipt_id=operation.id)
        else:
            if (
                kind == "verify"
                and not work.get("video_id")
                and not operation.remote_response.get("video_id")
            ):
                operation.remote_response = {
                    **operation.remote_response,
                    **_search_progress(work, evidence),
                }
            operation.status = (
                "verifying"
                if operation.remote_response.get("video_id")
                else "result_unknown"
            )
            attempt.status = operation.status
            _state(db, context, row, operation.status)
        _save_attempt_evidence(attempt, operation)
        operation.attempt_token, operation.claimed_until = None, None
        if operation.status != "succeeded":
            _queue(
                db,
                context=context,
                material_id=material_id,
                operation_id=operation.id,
                kind="verify",
                object_id=obj.id,
                generation=obj.generation,
                due=datetime.now(UTC) + timedelta(seconds=60),
            )


def _failure(
    database_engine: Any,
    *,
    context: TenantContext,
    material_id: UUID,
    object_id: UUID,
    generation: int,
    operation_id: UUID,
    claim: UUID,
    error: Exception,
    kind: str,
    post_attempted: bool,
) -> None:
    with Session(database_engine) as db, db.begin():
        _, obj, row = _records(
            db,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
        )
        operation = _locked_operation(db, context, operation_id)
        if operation.attempt_token != claim:
            return
        attempt = _attempt(db, operation_id)
        assert attempt
        code = (
            error.code
            if isinstance(error, DomainError)
            else "material_response_unknown"
        )
        deferred = isinstance(error, api.SdkAdmissionDeferred) or code in {
            "admission_unavailable",
            "source_capacity_pending",
        }
        armed = operation.remote_response.get("send_armed") is True
        if kind == "upload" and not post_attempted:
            # This still-running owner proved it never invoked the POST. A dead
            # owner cannot make this assertion; durable ARMED stays UNKNOWN.
            armed = False
            operation.remote_response = {
                **operation.remote_response,
                "send_armed": False,
            }
        operation.remote_response = {**operation.remote_response, "error_code": code}
        if armed:
            operation.status = (
                "verifying"
                if operation.remote_response.get("video_id")
                else "result_unknown"
            )
            attempt.status = operation.status
            _state(db, context, row, operation.status, code)
        elif deferred:
            # 当前claim持有者得到明确本地NOT_SENT后，才结束这次临时签名用途；字节预留不释放。
            if kind == "upload" and not post_attempted:
                _release_use(db, context=context, obj=obj, operation=operation)
            operation.status, attempt.status = "pending", "pending"
        else:
            operation.status, attempt.status = "failed", "blocked"
            db.flush()
            release_source_account(db, context=context, operation=operation)
            _release_use(db, context=context, obj=obj, operation=operation)
            _state(db, context, row, "failed", code)
        _save_attempt_evidence(attempt, operation)
        operation.attempt_token, operation.claimed_until = None, None
        if armed or deferred:
            _queue(
                db,
                context=context,
                material_id=material_id,
                operation_id=operation.id,
                kind="verify" if armed else kind,
                object_id=obj.id,
                generation=obj.generation,
                due=datetime.now(UTC) + timedelta(seconds=60),
            )


def run_url_source_upload(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    material_id: UUID,
    object_id: UUID,
    generation: int,
    kind: str = "upload",
    operation_id: UUID | None = None,
    s3: Any = None,
    recovery_claim_id: UUID | None = None,
    revision: int | None = None,
) -> None:
    if (
        kind not in {"upload", "verify"}
        or type(generation) is not int
        or generation < 1
    ):
        raise DomainError("invalid_asset_task", "素材任务代次参数无效")
    claim = uuid4()
    hard_limit = UPLOAD_HARD_LIMIT if kind == "upload" else READ_HARD_LIMIT
    claim_seconds = UPLOAD_CLAIM_SECONDS if kind == "upload" else READ_CLAIM_SECONDS
    deadline = datetime.now(UTC) + timedelta(seconds=hard_limit - 5)
    with Session(database_engine) as db, db.begin():
        material, obj, row = _records(
            db,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
        )
        if operation_id is None:
            if kind != "upload":
                raise DomainError("invalid_asset_task", "核实任务缺少操作标识")
            latest = db.exec(
                select(MaterialUploadAttempt)
                .join(
                    MaterialAssetOperation,
                    col(MaterialAssetOperation.id)
                    == col(MaterialUploadAttempt.operation_id),
                )
                .where(
                    MaterialUploadAttempt.tenant_id == context.tenant_id,
                    MaterialUploadAttempt.material_id == material_id,
                    col(MaterialAssetOperation.remote_response)["object_id"].as_string()
                    == str(object_id),
                    col(MaterialAssetOperation.remote_response)[
                        "generation"
                    ].as_integer()
                    == generation,
                )
                .order_by(
                    col(MaterialUploadAttempt.created_at).desc(),
                    col(MaterialUploadAttempt.id).desc(),
                )
                .limit(1)
            ).first()
            if latest:
                operation_id = latest.operation_id
            else:
                try:
                    with db.begin_nested():
                        require_tenant(
                            db,
                            tenant_id=context.tenant_id,
                            actor_id=context.actor_id,
                            action="upload",
                        )
                        _proof(material, obj, row, upload=True)
                        operation = _new_operation(
                            db, context=context, material=material, obj=obj
                        )
                        operation_id = operation.id
                except DomainError as error:
                    _state(db, context, row, "blocked", error.code)
                    if error.code in {
                        "source_capacity_pending",
                        "material_ingest_disabled",
                    }:
                        _queue(
                            db,
                            context=context,
                            material_id=material_id,
                            operation_id=None,
                            kind="upload",
                            object_id=object_id,
                            generation=generation,
                            due=datetime.now(UTC) + timedelta(seconds=60),
                        )
                    return
        operation = _locked_operation(db, context, operation_id)
        if (
            operation.material_id != material_id
            or operation.path != "upload_original"
            or operation.remote_response.get("object_id") != str(object_id)
            or operation.remote_response.get("generation") != generation
        ):
            raise DomainError("object_identity_unverified", "来源操作与原件代次不匹配")
        attempt = _attempt(db, operation.id)
        if attempt is None or operation.status in {"succeeded", "failed"}:
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
        expected = "verify" if operation.remote_response.get("send_armed") else "upload"
        if expected != kind:
            if operation.status == "sending":
                operation.status, attempt.status = "result_unknown", "result_unknown"
                operation.attempt_token, operation.claimed_until = None, None
                _queue(
                    db,
                    context=context,
                    material_id=material_id,
                    operation_id=operation.id,
                    kind="verify",
                    object_id=object_id,
                    generation=generation,
                )
            return
        operation.remote_response = {
            **operation.remote_response,
            "revision": operation.remote_response.get("revision", 0) + 1,
        }
        operation.attempt_token, operation.claimed_until = (
            claim,
            now + timedelta(seconds=claim_seconds),
        )
        _queue(
            db,
            context=context,
            material_id=material_id,
            operation_id=operation.id,
            kind=kind,
            object_id=object_id,
            generation=generation,
            due=operation.claimed_until,
            recovery_claim_id=claim,
        )
        work: dict[str, Any] = {
            **operation.remote_response,
            "frozen_route": operation.frozen_route,
            "bc_id": material.bc_id,
            "advertiser_id": operation.advertiser_id,
            "connection_id": attempt.connection_id,
            "md5": obj.video_md5 or "",
            "byte_size": obj.expected_bytes,
        }
    post_attempted = False
    evidence: dict[str, str] | _SearchPage | None
    try:
        route = load_material_route(
            work["frozen_route"],
            context=context,
            bc_id=work["bc_id"],
            connection_id=work["connection_id"],
        )
        logical_operation = (
            "materials.upload_video_url"
            if kind == "upload"
            else "materials.get_videos"
            if work.get("video_id")
            else "materials.search_videos"
        )
        policy = admission_policy(logical_operation)
        budget = material_types.RemoteCallBudget(
            deadline=deadline, hard_limit_seconds=hard_limit, lease_ms=policy.lease_ms
        )
        budget.timeout(upload=kind == "upload")

        def check_current() -> None:
            # 每个物理发送（包括MCP握手）都核查本地claim与代次，关闭短事务后才出网。
            with (
                bounded_session(database_engine, task_deadline=deadline) as check,
                check.begin(),
            ):
                material, obj, row = _records(
                    check,
                    context=context,
                    material_id=material_id,
                    object_id=object_id,
                    generation=generation,
                )
                operation = _locked_operation(check, context, operation_id)
                if operation.attempt_token != claim:
                    raise DomainError(
                        "material_claim_changed", "素材操作已由其他任务接管"
                    )
                _proof(material, obj, row, upload=kind == "upload")
                _source_access(
                    check, context=context, work=work, upload=kind == "upload"
                )
                if kind == "upload" and route.channel == "OFFICIAL_MCP":
                    require_url_upload(
                        material_upload_policy(
                            channel=route.channel,
                            adapter_contract_revision=route.adapter_contract_revision,
                        ),
                        byte_size=work["byte_size"],
                    )
                budget.timeout(upload=kind == "upload")

        check_current()
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            route=route,
            task_deadline=deadline,
            before_request=check_current,
        ) as gateway:
            if kind == "upload":
                with (
                    bounded_session(database_engine, task_deadline=deadline) as arm,
                    arm.begin(),
                ):
                    material, obj, row = _records(
                        arm,
                        context=context,
                        material_id=material_id,
                        object_id=object_id,
                        generation=generation,
                    )
                    operation = _locked_operation(arm, context, operation_id)
                    if operation.attempt_token != claim:
                        return
                    _proof(material, obj, row, upload=True)
                    _source_access(arm, context=context, work=work)
                    if route.channel == "OFFICIAL_MCP":
                        require_url_upload(
                            material_upload_policy(
                                channel=route.channel,
                                adapter_contract_revision=route.adapter_contract_revision,
                            ),
                            byte_size=work["byte_size"],
                        )
                    operation.status = "sending"
                    operation.remote_response = {
                        **operation.remote_response,
                        "send_armed": True,
                    }
                    attempt = _attempt(arm, operation_id)
                    assert attempt
                    attempt.status = "uploading"
                    _state(arm, context, row, "uploading")
                url = sign_ingest_url(
                    database_engine=database_engine,
                    context=context,
                    object_id=object_id,
                    operation_id=operation_id,
                    s3=s3,
                )
                request = material_types.URLVideoUpload(
                    work["advertiser_id"],
                    url,
                    work["remote_name"],
                    work["md5"],
                    work["byte_size"],
                )
                post_attempted = True
                receipt = gateway.materials.upload_video_url(request, budget=budget)
                evidence = api.receipt_evidence(receipt)
                # 回执只白名单保存ID；先提交后退出gateway，不让清理错误抹掉已知身份。
                for receipt_attempt in range(2):
                    try:
                        _receipt(
                            database_engine,
                            context=context,
                            material_id=material_id,
                            object_id=object_id,
                            generation=generation,
                            operation_id=operation_id,
                            claim=claim,
                            evidence=evidence,
                        )
                        break
                    except SDK_SCOPE_INTERRUPTS:
                        raise
                    except Exception:
                        if receipt_attempt:
                            raise
            elif work.get("video_id"):
                record = gateway.materials.read_video(
                    advertiser_id=work["advertiser_id"],
                    video_id=work["video_id"],
                    budget=budget,
                )
                evidence = api.video_identity(
                    record,
                    advertiser_id=work["advertiser_id"],
                    video_id=work["video_id"],
                    md5=work["md5"],
                    expected_size=work["byte_size"],
                )
            else:
                page = work.get("search_page", 1)
                result = gateway.materials.search_videos(
                    advertiser_id=work["advertiser_id"],
                    page=page,
                    material_ids=(),
                    budget=budget,
                )
                if (
                    not 0 <= result.total_pages <= MAX_SEARCH_PAGES
                    or not 1 <= page <= MAX_SEARCH_PAGES
                ):
                    raise DomainError(
                        "material_reconciliation_bounded", "来源核查超过有界分页范围"
                    )
                if result.total_number is None:
                    raise DomainError(
                        "material_result_pending", "来源核查缺少完整目录数量证明"
                    )
                matches = []
                for record in result.rows:
                    if record.file_name == work["remote_name"]:
                        match = api.video_identity(
                            record,
                            advertiser_id=work["advertiser_id"],
                            video_id=record.video_id,
                            md5=work["md5"],
                            expected_size=work["byte_size"],
                        )
                        if match:
                            matches.append(match)
                evidence = _SearchPage(
                    page=page,
                    total_pages=result.total_pages,
                    total_number=result.total_number,
                    ids=tuple(record.video_id for record in result.rows),
                    matches=matches,
                )
        _finish(
            database_engine,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
            operation_id=operation_id,
            claim=claim,
            work=work,
            kind=kind,
            evidence=evidence,
        )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as error:
        if isinstance(error, api.SdkAdmissionDeferred) or (
            isinstance(error, RemoteCallError) and error.effect == "NOT_SENT"
        ):
            post_attempted = False
        _failure(
            database_engine,
            context=context,
            material_id=material_id,
            object_id=object_id,
            generation=generation,
            operation_id=operation_id,
            claim=claim,
            error=error,
            kind=kind,
            post_attempted=post_attempted,
        )
