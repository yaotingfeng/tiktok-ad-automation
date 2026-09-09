"""Submission-only target preparation; source and target share one send authority."""

from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import sdk_client
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from . import sdk_assets as api
from .models import AccountMaterial, MaterialAssetOperation, MaterialDistribution
from .readiness import (
    get_material_readiness,
    load_material,
    mapping_fresh,
    require_execution_config,
    require_upload_path,
    target_mapping,
)
from .schemas import AssetPreparation
from .source_uploads import (
    READ_CLAIM_SECONDS,
    READ_HARD_LIMIT,
    UNRESOLVED,
    UPLOAD_CLAIM_SECONDS,
    UPLOAD_HARD_LIMIT,
    _attempt,
    _locked_material,
    _locked_operation,
    remote_name,
    reserve_asset_operation,
)
from .storage import OriginalFile, open_original

register_dispatch_task("materials.prepare_target", "resources")
register_dispatch_task("materials.verify_target", "resources")
ACTIVE_DISTRIBUTIONS = ("queued", "preparing", "verifying", "result_unknown")


def _context(dist: MaterialDistribution) -> TenantContext:
    return TenantContext(
        tenant_id=dist.tenant_id, actor_id=dist.actor_id, role="operator"
    )


def queue_distribution(
    session: Session,
    dist: MaterialDistribution,
    operation: MaterialAssetOperation,
    *,
    kind: str,
    due: datetime | None = None,
    claim_id: UUID | None = None,
    observe: bool = False,
    read_only: bool = False,
) -> None:
    if read_only and kind != "verify":
        raise DomainError("invalid_asset_task", "只读核实不能安排上传")
    payload: dict[str, Any] = {
        "distribution_id": str(dist.id),
        "operation_id": str(operation.id),
    }
    if observe:
        payload["observe"] = True
        key = f"material-target-observe:{dist.id}"
    elif claim_id:
        payload["claim_id"] = str(claim_id)
        key = f"material-target-recover:{dist.id}:{claim_id}"
    else:
        revision = operation.remote_response.get("revision", 0)
        payload["revision"] = revision
        key = f"material-target:{dist.id}:{operation.id}:{revision}:{kind}"
    if read_only:
        payload["read_only"] = True
        key += f":read:{operation.id}"
        if observe:
            payload["revision"] = operation.remote_response.get("revision", 0)
            key += f":{payload['revision']}"
    existing = session.exec(
        select(PendingDispatch)
        .where(
            PendingDispatch.tenant_id == dist.tenant_id,
            PendingDispatch.task_key == key,
        )
        .with_for_update()
    ).first()
    if existing:
        if (existing.actor_id, existing.task_name, existing.payload) != (
            dist.actor_id,
            f"materials.{kind}_target",
            payload,
        ):
            raise DomainError("dispatch_payload_invalid", "素材核实投递身份不匹配")
        # Reuse one observation record; never invalidate an unpublished message
        # or reset broker backoff while waiting for a source-owned operation.
        if observe and existing.published_at is not None:
            existing.published_at = None
            existing.available_at = due or datetime.now(UTC)
        return
    identity = enqueue_after_commit(
        session,
        context=_context(dist),
        task_name=f"materials.{kind}_target",
        task_key=key,
        payload=payload,
    )
    if due:
        dispatch = session.get(PendingDispatch, identity)
        assert dispatch
        dispatch.available_at = due


def _bind_operation(
    session: Session,
    *,
    context: TenantContext,
    material_id: UUID,
    bc_id: str,
    advertiser_id: str,
    path: str,
) -> MaterialAssetOperation:
    operation = reserve_asset_operation(
        session,
        context=context,
        material_id=material_id,
        advertiser_id=advertiser_id,
        path="upload_original",
        action="build",
    )
    if _attempt(session, operation.id) is None and operation.status == "pending":
        mapping = target_mapping(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            advertiser_id=advertiser_id,
        )
        if path == "existing_target" and mapping and mapping.video_id.strip():
            operation.status = "verifying"
            operation.remote_response = {
                **operation.remote_response,
                "video_id": mapping.video_id,
                "read_only": True,
            }
            if mapping.mid:
                operation.remote_response = {
                    **operation.remote_response,
                    "mid": mapping.mid,
                }
    return operation


def ensure_target_asset(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    advertiser_id: str,
    task_key: str,
) -> AssetPreparation:
    """Internal build-submission boundary. Caller commits; no browser write route."""
    if not isinstance(task_key, str) or not task_key.strip() or len(task_key) > 255:
        raise DomainError("invalid_asset_task", "搭建素材步骤标识无效")
    # The file lock serializes both two build consumers and source reservation.
    load_material(
        session, context=context, bc_id=bc_id, material_id=material_id, lock=True
    )
    readiness = get_material_readiness(
        session,
        context=context,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
    )
    if readiness.state == "ready":
        return AssetPreparation(state="ready", mapping=readiness.mapping)
    if readiness.state == "blocked":
        return AssetPreparation(
            state="blocked",
            reason_code=readiness.reason_code,
            reason_message=readiness.reason_message,
        )
    existing = session.exec(
        select(MaterialDistribution)
        .where(
            MaterialDistribution.tenant_id == context.tenant_id,
            MaterialDistribution.material_id == material_id,
            MaterialDistribution.advertiser_id == advertiser_id,
            col(MaterialDistribution.status).in_(ACTIVE_DISTRIBUTIONS),
        )
        .with_for_update()
    ).first()
    if existing:
        return AssetPreparation(state="queued", task_id=existing.id)
    operation = _bind_operation(
        session,
        context=context,
        material_id=material_id,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        path=readiness.path,
    )
    dist = MaterialDistribution(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        actor_id=context.actor_id,
        operation_id=operation.id,
        path=operation.path
        if operation.status in {"sending", "result_unknown"}
        else readiness.path,
    )
    session.add(dist)
    session.flush()
    source_owned = _attempt(session, operation.id) is not None
    kind = (
        "verify"
        if source_owned
        or operation.status in {"sending", "result_unknown", "verifying"}
        else "prepare"
    )
    queue_distribution(session, dist, operation, kind=kind, observe=source_owned)
    return AssetPreparation(state="queued", task_id=dist.id)


def _load_distribution(
    session: Session, context: TenantContext, distribution_id: UUID
) -> MaterialDistribution:
    dist = session.exec(
        select(MaterialDistribution)
        .where(
            MaterialDistribution.tenant_id == context.tenant_id,
            MaterialDistribution.id == distribution_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if dist is None:
        raise DomainError("material_distribution_not_found", "未找到目标素材任务")
    if dist.actor_id != context.actor_id:
        raise DomainError("tenant_forbidden", "任务操作人不匹配")
    return dist


def _target_access(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    *,
    upload: bool,
) -> AccountAccess:
    build = resolve_account_access(
        session,
        context=context,
        bc_id=dist.bc_id,
        advertiser_id=dist.advertiser_id,
        action="build",
    )
    if upload:
        return resolve_account_access(
            session,
            context=context,
            bc_id=dist.bc_id,
            advertiser_id=dist.advertiser_id,
            action="upload",
        )
    return build


def _publish_mapping(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    evidence: dict[str, str],
) -> None:
    access = _target_access(session, context, dist, upload=False)
    mapping = target_mapping(
        session,
        context=context,
        bc_id=dist.bc_id,
        material_id=dist.material_id,
        advertiser_id=dist.advertiser_id,
    )
    if mapping is None:
        mapping = AccountMaterial(
            tenant_id=dist.tenant_id,
            bc_id=dist.bc_id,
            material_id=dist.material_id,
            advertiser_id=dist.advertiser_id,
            connection_id=access.connection_id,
            video_id=evidence["video_id"],
        )
        session.add(mapping)
    if mapping.video_id != evidence["video_id"]:
        mapping.image_id = mapping.cover_url = None
    mapping.video_id, mapping.mid = evidence["video_id"], evidence.get("mid")
    mapping.connection_id = access.connection_id
    mapping.status, mapping.verified_at = "available", datetime.now(UTC)


def _blocked(dist: MaterialDistribution, code: str) -> None:
    dist.status, dist.reason_code = "blocked", code


def run_distribution(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    kind: str,
    operation_id: UUID | None = None,
    revision: int | None = None,
    recovery_claim_id: UUID | None = None,
    s3: Any = None,
    read_only: bool = False,
) -> None:
    """One official call at most; production entrypoint enforces a process deadline."""
    if kind not in {"prepare", "verify"} or (read_only and kind != "verify"):
        raise DomainError("invalid_asset_task", "目标素材工作任务无效")
    claim = uuid4()
    hard = UPLOAD_HARD_LIMIT if kind == "prepare" else READ_HARD_LIMIT
    lease = UPLOAD_CLAIM_SECONDS if kind == "prepare" else READ_CLAIM_SECONDS
    deadline = datetime.now(UTC) + timedelta(seconds=hard - 5)
    with Session(database_engine) as session, session.begin():
        dist = _load_distribution(session, context, distribution_id)
        if dist.status not in ACTIVE_DISTRIBUTIONS and not (
            read_only and dist.status in {"ready", "blocked"}
        ):
            return
        try:
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="build",
            )
            material = load_material(
                session,
                context=context,
                bc_id=dist.bc_id,
                material_id=dist.material_id,
                lock=True,
            )
            _target_access(session, context, dist, upload=False)
        except DomainError as error:
            _blocked(dist, error.code)
            return
        assert dist.operation_id
        if operation_id is not None and operation_id != dist.operation_id:
            return
        operation = _locked_operation(session, context, dist.operation_id)
        if (
            read_only
            and revision is not None
            and revision != operation.remote_response.get("revision", 0)
        ):
            return
        source = _attempt(session, operation.id)
        mapping = target_mapping(
            session,
            context=context,
            bc_id=dist.bc_id,
            material_id=dist.material_id,
            advertiser_id=dist.advertiser_id,
        )
        if (
            not read_only
            and mapping_fresh(mapping)
            and (source or operation.status in {"pending", "succeeded"})
        ):
            dist.status, dist.reason_code = "ready", None
            return
        if read_only and operation.status in {"failed", "pending", "confirmed_absent"}:
            # A delayed manual read cannot acquire a new upload authority. Keep
            # the actual operation history intact even after definitive failure.
            if operation.status == "failed":
                _blocked(dist, "material_operation_failed")
            return
        if read_only and operation.status == "succeeded":
            if revision is not None and revision != operation.remote_response.get(
                "revision", 0
            ):
                return
            if operation.claimed_until and operation.claimed_until > datetime.now(UTC):
                return
            known_id = operation.remote_response.get("video_id") or (
                mapping.video_id if mapping else None
            )
            if not isinstance(known_id, str) or not known_id.strip():
                return
            # Revalidate the actual receipt in the same fenced operation. This
            # never reserves a fresh upload or changes source-account history.
            operation.remote_response = {
                **operation.remote_response,
                "video_id": known_id,
            }
            operation.status, dist.status = "verifying", "verifying"
            source = None
        if source:
            if (
                operation.status == "failed"
                and source.status in {"failed", "blocked"}
                and not operation.remote_response.get("video_id")
            ):
                try:
                    require_upload_path(
                        session,
                        context=context,
                        material=material,
                        advertiser_id=dist.advertiser_id,
                    )
                except DomainError as error:
                    _blocked(dist, error.code)
                    return
                operation = _bind_operation(
                    session,
                    context=context,
                    material_id=material.id,
                    bc_id=dist.bc_id,
                    advertiser_id=dist.advertiser_id,
                    path="upload_original",
                )
                dist.operation_id, dist.path = operation.id, "upload_original"
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="prepare"
                )
            elif operation.status == "succeeded":
                # The source completed while an observation was delayed beyond
                # the cache window. Revalidate under a new target-owned read.
                operation = _bind_operation(
                    session,
                    context=context,
                    material_id=material.id,
                    bc_id=dist.bc_id,
                    advertiser_id=dist.advertiser_id,
                    path="existing_target",
                )
                dist.operation_id, dist.path = operation.id, "existing_target"
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="verify"
                )
            else:
                dist.status = (
                    "result_unknown"
                    if operation.status == "result_unknown"
                    else "verifying"
                )
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind="verify",
                    observe=True,
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
            return
        if recovery_claim_id and operation.attempt_token != recovery_claim_id:
            return
        if revision is not None and revision != operation.remote_response.get(
            "revision", 0
        ):
            return
        if operation.claimed_until and operation.claimed_until > datetime.now(UTC):
            return
        expired_send = operation.status == "sending"
        if expired_send:
            operation.status, dist.status = "result_unknown", "result_unknown"
        if operation.status == "failed":
            if not operation.remote_response.get("definite_no_effect"):
                _blocked(dist, "material_operation_failed")
                return
            try:
                require_upload_path(
                    session,
                    context=context,
                    material=material,
                    advertiser_id=dist.advertiser_id,
                )
            except DomainError as error:
                _blocked(dist, error.code)
                return
            operation = _bind_operation(
                session,
                context=context,
                material_id=material.id,
                bc_id=dist.bc_id,
                advertiser_id=dist.advertiser_id,
                path="upload_original",
            )
            dist.operation_id, dist.path = operation.id, "upload_original"
            if kind != "prepare":
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="prepare"
                )
                return
        if operation.status not in UNRESOLVED:
            return
        expected = (
            "prepare"
            if operation.status in {"pending", "confirmed_absent"}
            else "verify"
        )
        if expected != kind:
            if expired_send:
                operation.attempt_token, operation.claimed_until = None, None
                queue_distribution(
                    session, dist, operation, read_only=read_only, kind="verify"
                )
            return
        try:
            if kind == "prepare":
                # No verified native sharing capability exists in this deployment.
                # Only an UNSENT share may safely choose original upload instead.
                require_upload_path(
                    session,
                    context=context,
                    material=material,
                    advertiser_id=dist.advertiser_id,
                )
                if operation.path == "share_source":
                    operation.path, dist.path = "upload_original", "upload_original"
                    operation.remote_response = {
                        **operation.remote_response,
                        "share_disabled_before_send": True,
                    }
                endpoint = api.UPLOAD_ENDPOINT
            else:
                if not material.video_md5:
                    raise DomainError(
                        "material_digest_missing", "素材缺少可核实内容摘要"
                    )
                endpoint = (
                    api.INFO_ENDPOINT
                    if operation.remote_response.get("video_id")
                    else api.SEARCH_ENDPOINT
                )
                require_execution_config(upload=False, endpoint=endpoint)
        except DomainError as error:
            _blocked(dist, error.code)
            if kind == "prepare":
                operation.status = "failed"
                operation.remote_response = {
                    **operation.remote_response,
                    "definite_no_effect": True,
                    "error_code": error.code,
                }
            return
        if read_only:
            dist.status = "verifying"
        previous_status = operation.status
        current_revision = operation.remote_response.get("revision", 0) + 1
        operation.remote_response = {
            **operation.remote_response,
            "revision": current_revision,
        }
        operation.attempt_token, operation.claimed_until = (
            claim,
            datetime.now(UTC) + timedelta(seconds=lease),
        )
        operation_id = operation.id
        queue_distribution(
            session,
            dist,
            operation,
            read_only=read_only,
            kind=kind,
            due=operation.claimed_until,
            claim_id=claim,
        )
        work: dict[str, Any] = {
            **operation.remote_response,
            "bc_id": dist.bc_id,
            "material_id": material.id,
            "advertiser_id": dist.advertiser_id,
            "remote_name": remote_name(material),
        }
        content_md5 = material.video_md5 or ""
    sent = False
    evidence: dict[str, str] | tuple[list[dict[str, str]], bool] | None = None
    original_scope: AbstractContextManager[OriginalFile | None]
    try:
        original_scope = (
            open_original(
                database_engine=database_engine,
                context=context,
                bc_id=work["bc_id"],
                material_id=work["material_id"],
                action="build",
                deadline=deadline,
                s3=s3,
            )
            if kind == "prepare"
            else nullcontext(None)
        )
        with original_scope as original:
            policy = admission_policy(endpoint)
            # Repeat configuration checks after original I/O, before admission.
            require_execution_config(upload=kind == "prepare", endpoint=endpoint)
            with api.admitted_asset_call(
                redis_client,
                context=context,
                endpoint=endpoint,
                advertiser_id=work["advertiser_id"],
                policy=policy,
            ):
                with Session(database_engine) as session:
                    dist = _load_distribution(session, context, distribution_id)
                    current_material = load_material(
                        session,
                        context=context,
                        bc_id=dist.bc_id,
                        material_id=dist.material_id,
                        lock=True,
                    )
                    operation = _locked_operation(session, context, operation_id)
                    if (
                        operation.attempt_token != claim
                        or dist.operation_id != operation_id
                    ):
                        return
                    access = _target_access(
                        session, context, dist, upload=kind == "prepare"
                    )
                    with sdk_client(
                        session, context=context, connection_id=access.connection_id
                    ) as client:
                        if datetime.now(UTC) >= deadline:
                            raise DomainError(
                                "material_deadline", "素材处理已到达本次期限"
                            )
                        if original:
                            current_material.sha256, current_material.video_md5 = (
                                original.sha256,
                                original.md5,
                            )
                            content_md5 = original.md5
                            operation.request_digest = sha256(
                                f"{work['advertiser_id']}:{work['remote_name']}:{original.sha256}".encode()
                            ).hexdigest()
                            operation.status, dist.status = "sending", "preparing"
                            operation.remote_response = {
                                **operation.remote_response,
                                "upload_connection_id": str(access.connection_id),
                            }
                        session.commit()
                        session.close()
                        sent = True
                        if original:
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
            dist = _load_distribution(session, context, distribution_id)
            _locked_material(session, context, dist.material_id)
            operation = _locked_operation(session, context, operation_id)
            if operation.attempt_token != claim or dist.operation_id != operation_id:
                return
            if kind == "prepare":
                assert isinstance(evidence, dict)
                operation.remote_response = {
                    **operation.remote_response,
                    **evidence,
                    "upload_video_id": evidence["video_id"],
                    "upload_mid": evidence.get("mid"),
                }
                operation.status, dist.status = "verifying", "verifying"
            elif work.get("video_id") and evidence:
                assert isinstance(evidence, dict)
                _publish_mapping(session, context, dist, evidence)
                operation.remote_response = {**operation.remote_response, **evidence}
                operation.status, dist.status = "succeeded", "ready"
            elif not work.get("video_id"):
                assert isinstance(evidence, tuple)
                matches, last = evidence
                candidates = {
                    item["video_id"]: item for item in work.get("candidates", [])
                }
                candidates.update({item["video_id"]: item for item in matches})
                if len(candidates) > 1:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1,
                        "candidates": [],
                        "error_code": "material_reconciliation_ambiguous",
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
                elif last and candidates:
                    operation.remote_response = {
                        **operation.remote_response,
                        **next(iter(candidates.values())),
                        "candidates": [],
                    }
                    operation.status, dist.status = "verifying", "verifying"
                else:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1 if last else work.get("search_page", 1) + 1,
                        "candidates": [] if last else list(candidates.values()),
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
            else:
                operation.status, dist.status = "verifying", "verifying"
            dist.reason_code = (
                None if dist.status == "ready" else "material_result_pending"
            )
            operation.attempt_token, operation.claimed_until = None, None
            if dist.status != "ready":
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind="verify",
                    due=datetime.now(UTC) + timedelta(seconds=60),
                )
    except Exception as error:
        with Session(database_engine) as session, session.begin():
            dist = _load_distribution(session, context, distribution_id)
            _locked_material(session, context, dist.material_id)
            operation = _locked_operation(session, context, operation_id)
            if operation.attempt_token != claim or dist.operation_id != operation_id:
                return
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
            if operation.status == "failed":
                operation.remote_response = {
                    **operation.remote_response,
                    "definite_no_effect": True,
                }
            operation.remote_response = {
                **operation.remote_response,
                "error_code": code,
            }
            operation.attempt_token, operation.claimed_until = None, None
            dist.status = (
                "queued"
                if deferred
                else "blocked"
                if operation.status == "failed"
                else "result_unknown"
            )
            dist.reason_code = code
            if dist.status != "blocked":
                delay = (
                    max(1, error.retry_after_ms) / 1000
                    if isinstance(error, api.SdkAdmissionDeferred)
                    else 60
                )
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind=kind if deferred else "verify",
                    due=datetime.now(UTC) + timedelta(seconds=delay),
                )


def repair_material_dispatches(session: Session, *, limit: int = 100) -> int:
    """Bounded repair of lost material messages, preserving their exact generation.

    Published is broker acceptance, not execution. Re-arm the same active business
    message after a grace period; never manufacture revisions or touch pending
    broker backoff. Eligibility is filtered in SQL before applying the row limit.
    """
    from sqlalchemy import String, and_, cast, func, or_

    from .models import MaterialFile, MaterialUploadAttempt, ObjectUpload

    if type(limit) is not int or not 0 < limit <= 100:
        raise DomainError("invalid_asset_task", "素材恢复批量上限为 100")
    now = datetime.now(UTC)
    payload = col(PendingDispatch.payload)
    remote = col(MaterialAssetOperation.remote_response)
    op_owner = (
        select(MaterialUploadAttempt.id)
        .where(
            MaterialUploadAttempt.operation_id == MaterialAssetOperation.id,
            MaterialUploadAttempt.tenant_id == MaterialAssetOperation.tenant_id,
        )
        .exists()
    )
    live_message = or_(
        and_(
            col(MaterialAssetOperation.attempt_token).is_not(None),
            payload["claim_id"].astext
            == cast(col(MaterialAssetOperation.attempt_token), String),
            col(MaterialAssetOperation.claimed_until) <= now,
        ),
        and_(
            col(MaterialAssetOperation.attempt_token).is_(None),
            payload["revision"].astext == func.coalesce(remote["revision"].astext, "0"),
        ),
    )
    source_work = (
        select(MaterialAssetOperation.id)
        .where(
            MaterialAssetOperation.tenant_id == PendingDispatch.tenant_id,
            payload["operation_id"].astext
            == cast(col(MaterialAssetOperation.id), String),
            col(PendingDispatch.task_name).in_(
                ["materials.upload_original", "materials.verify_original"]
            ),
            col(MaterialAssetOperation.status).in_(UNRESOLVED),
            op_owner,
            live_message,
        )
        .exists()
    )
    source_started = (
        select(MaterialUploadAttempt.id)
        .where(
            MaterialUploadAttempt.tenant_id == ObjectUpload.tenant_id,
            MaterialUploadAttempt.material_id == ObjectUpload.material_id,
        )
        .exists()
    )
    original_work = (
        select(ObjectUpload.id)
        .join(
            MaterialFile,
            and_(
                col(ObjectUpload.material_id) == MaterialFile.id,
                col(ObjectUpload.tenant_id) == MaterialFile.tenant_id,
            ),
        )
        .where(
            ObjectUpload.tenant_id == PendingDispatch.tenant_id,
            ObjectUpload.task_id == PendingDispatch.id,
            PendingDispatch.task_name == "materials.upload_original",
            ObjectUpload.status == "stored",
            MaterialFile.storage_state == "stored",
            ~source_started,
        )
        .exists()
    )
    target_work = (
        select(MaterialDistribution.id)
        .join(
            MaterialAssetOperation,
            and_(
                col(MaterialDistribution.operation_id) == MaterialAssetOperation.id,
                col(MaterialDistribution.tenant_id) == MaterialAssetOperation.tenant_id,
            ),
        )
        .where(
            MaterialDistribution.tenant_id == PendingDispatch.tenant_id,
            payload["distribution_id"].astext
            == cast(col(MaterialDistribution.id), String),
            payload["operation_id"].astext
            == cast(col(MaterialAssetOperation.id), String),
            col(PendingDispatch.task_name).in_(
                ["materials.prepare_target", "materials.verify_target"]
            ),
            or_(
                col(MaterialDistribution.status).in_(ACTIVE_DISTRIBUTIONS),
                and_(
                    payload["read_only"].astext == "true",
                    col(MaterialDistribution.status).in_(["ready", "blocked"]),
                ),
            ),
            or_(
                and_(payload["read_only"].astext == "true", live_message),
                and_(
                    payload["observe"].astext == "true",
                    payload["read_only"].astext.is_distinct_from("true"),
                    op_owner,
                ),
                and_(col(MaterialAssetOperation.status).in_(UNRESOLVED), live_message),
            ),
        )
        .exists()
    )
    rows = session.exec(
        select(PendingDispatch)
        .where(
            col(PendingDispatch.published_at) <= now - timedelta(seconds=120),
            PendingDispatch.available_at <= now,
            or_(source_work, original_work, target_work),
        )
        .order_by(col(PendingDispatch.published_at), col(PendingDispatch.id))
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for dispatch in rows:
        dispatch.published_at = None
    session.flush()
    return len(rows)
