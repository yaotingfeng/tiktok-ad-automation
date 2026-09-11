"""Submission-only target preparation; source and target share one send authority."""

from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.material_upload_evidence import material_upload_policy
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.routing import freeze_route
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from . import sdk_assets as api
from .channel_policy import require_url_upload
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from .readiness import (
    get_material_readiness,
    load_material,
    mapping_fresh,
    require_execution_config,
    require_upload_path,
    target_mapping,
)
from .remote_sources import (
    read_frozen_remote_source,
    require_remote_material,
    resolve_remote_source,
)
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
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
    route: FrozenTikTokRoute,
) -> MaterialAssetOperation:
    operation = reserve_asset_operation(
        session,
        context=context,
        material_id=material_id,
        advertiser_id=advertiser_id,
        path="share_source" if path == "share_source" else "upload_original",
        action="build",
        route=route,
    )
    if _attempt(session, operation.id) is None and operation.status == "pending":
        if (
            path == "share_source"
            and operation.path == "share_source"
            and not operation.remote_response
        ):
            material = load_material(
                session, context=context, bc_id=bc_id, material_id=material_id
            )
            source = resolve_remote_source(
                session,
                context=context,
                bc_id=bc_id,
                material_id=material_id,
                target_advertiser_id=advertiser_id,
            )
            if source is None:
                raise DomainError(
                    "material_remote_source_unavailable", "请恢复来源授权或补传原件"
                )
            operation.remote_response = {
                "transport": "url_relay",
                "source_asset_id": str(source.id),
                "source_advertiser_id": source.advertiser_id,
                "source_connection_id": str(source.connection_id),
                "source_video_id": source.video_id,
                "remote_name": f"{material.id}-{operation.id}.mp4",
                "content_md5": material.video_md5,
            }
            operation.request_digest = sha256(
                f"{operation.request_digest}:{source.id}:{source.video_id}:{material.video_md5}:{operation.id}".encode()
            ).hexdigest()
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
                "verification_connection_id": str(mapping.connection_id),
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
    route: FrozenTikTokRoute,
) -> AssetPreparation:
    """Internal build-submission boundary. Caller commits; no browser write route."""
    if not isinstance(task_key, str) or not task_key.strip() or len(task_key) > 255:
        raise DomainError("invalid_asset_task", "搭建素材步骤标识无效")
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
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
        route=route,
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
        require_same_route(
            load_material_route(existing.target_route, context=context, bc_id=bc_id),
            route,
        )
        return AssetPreparation(state="queued", task_id=existing.id)
    operation = _bind_operation(
        session,
        context=context,
        material_id=material_id,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        path=readiness.path,
        route=route,
    )
    source_route = None
    source_asset_id = operation.remote_response.get("source_asset_id")
    if source_asset_id:
        source = resolve_remote_source(
            session,
            context=context,
            bc_id=bc_id,
            material_id=material_id,
            source_asset_id=UUID(source_asset_id),
        )
        if source is None:
            raise DomainError(
                "material_remote_source_unavailable", "来源证据或权限已改变"
            )
        previous_dependency = session.exec(
            select(MaterialDistribution)
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                MaterialDistribution.operation_id == operation.id,
            )
            .order_by(col(MaterialDistribution.id))
            .limit(1)
        ).first()
        # 同一操作的后续消费者继承原来源依赖，不能借新 distribution 刷新旧授权版本。
        source_route = (
            load_material_route(
                previous_dependency.source_route,
                context=context,
                bc_id=bc_id,
                connection_id=source.connection_id,
            )
            if previous_dependency
            else freeze_route(
                session,
                context=context,
                bc_id=bc_id,
                connection_id=source.connection_id,
            )
        )
        require_material_route(
            session,
            context=context,
            route=source_route,
            bc_id=bc_id,
            advertiser_id=source.advertiser_id,
            capability="read",
        )
    dist = MaterialDistribution(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        material_id=material_id,
        advertiser_id=advertiser_id,
        actor_id=context.actor_id,
        operation_id=operation.id,
        target_route=route.model_dump(mode="json"),
        source_route=source_route.model_dump(mode="json") if source_route else None,
        source_asset_id=UUID(source_asset_id) if source_asset_id else None,
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
    connection_id: UUID | None = None,
) -> AccountAccess:
    route = load_material_route(dist.target_route, context=context, bc_id=dist.bc_id)
    if connection_id is not None and connection_id != route.connection_id:
        raise DomainError("frozen_route_changed", "素材请求连接与原任务不一致")
    for action in ("build", "upload") if upload else ("build",):
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=dist.bc_id,
            advertiser_id=dist.advertiser_id,
            capability=action,
        )
    return resolve_account_access(
        session,
        context=context,
        bc_id=dist.bc_id,
        advertiser_id=dist.advertiser_id,
        action="upload" if upload else "build",
        connection_id=route.connection_id,
    )


def _publish_mapping(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    evidence: dict[str, str],
    *,
    connection_id: UUID | None = None,
) -> None:
    access = _target_access(
        session, context, dist, upload=False, connection_id=connection_id
    )
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
        mapping.connection_id = access.connection_id
    mapping.video_id, mapping.mid = evidence["video_id"], evidence.get("mid")
    mapping.status, mapping.verified_at = "available", datetime.now(UTC)


def _blocked(dist: MaterialDistribution, code: str) -> None:
    dist.status, dist.reason_code = "blocked", code


def _work_connection(work: dict[str, Any]) -> UUID:
    return FrozenTikTokRoute.model_validate(work["target_route"]).connection_id


def _require_relay(
    session: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    operation: MaterialAssetOperation,
    source_route: FrozenTikTokRoute,
) -> None:
    require_remote_material(material)
    if operation.remote_response.get("transport") != "url_relay":
        raise DomainError(
            "material_share_unverified", "原生共享能力未经核实，不能发送或自动切换路径"
        )
    if material.byte_size > settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
        raise DomainError(
            "url_upload_capacity_exceeded", "素材超过当前URL转存工程容量限制"
        )
    source = resolve_remote_source(
        session,
        context=context,
        bc_id=material.bc_id,
        material_id=material.id,
        source_asset_id=UUID(operation.remote_response["source_asset_id"]),
    )
    if (
        source is None
        or (source.advertiser_id, str(source.connection_id), source.video_id)
        != (
            operation.remote_response.get("source_advertiser_id"),
            operation.remote_response.get("source_connection_id"),
            operation.remote_response.get("source_video_id"),
        )
        or operation.remote_response.get("content_md5") != material.video_md5
    ):
        raise DomainError("material_remote_source_unavailable", "来源证据或权限已改变")
    require_material_route(
        session,
        context=context,
        route=source_route,
        bc_id=material.bc_id,
        advertiser_id=source.advertiser_id,
        capability="read",
    )
    if source_route.connection_id != source.connection_id:
        raise DomainError("frozen_route_changed", "来源连接已改变")
    target_route = load_material_route(
        operation.frozen_route, context=context, bc_id=material.bc_id
    )
    if target_route.channel == "OFFICIAL_MCP":
        require_url_upload(
            material_upload_policy(
                channel=target_route.channel,
                adapter_contract_revision=target_route.adapter_contract_revision,
            ),
            byte_size=material.byte_size,
        )
    require_execution_config(
        upload=True,
        endpoint="materials.upload_video_url",
        original=False,
        channel=target_route.channel,
    )


def _relay_receipt(
    database_engine: Any,
    *,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID,
    claim: UUID,
    evidence: dict[str, str],
) -> None:
    with Session(database_engine) as db, db.begin():
        dist = _load_distribution(db, context, distribution_id)
        _locked_material(db, context, dist.material_id)
        operation = _locked_operation(db, context, operation_id)
        existing = operation.remote_response.get("video_id")
        if existing and existing != evidence["video_id"]:
            operation.remote_response = {
                **operation.remote_response,
                "conflicting_video_id": evidence["video_id"],
                "error_code": "material_reconciliation_ambiguous",
            }
            return
        operation.remote_response = {
            **operation.remote_response,
            **evidence,
            "upload_video_id": evidence["video_id"],
            "upload_mid": evidence.get("mid"),
        }
        if operation.attempt_token == claim and dist.operation_id == operation_id:
            operation.status, dist.status = "verifying", "verifying"


def _send_relay(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    distribution_id: UUID,
    operation_id: UUID,
    claim: UUID,
    work: dict[str, Any],
    deadline: datetime,
    hard: int,
) -> dict[str, str] | None:
    preview = read_frozen_remote_source(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        bc_id=work["bc_id"],
        material_id=work["material_id"],
        source_asset_id=UUID(work["source_asset_id"]),
        route=load_material_route(
            work["source_route"], context=context, bc_id=work["bc_id"]
        ),
        deadline=deadline,
    )
    route = load_material_route(
        work["target_route"], context=context, bc_id=work["bc_id"]
    )
    policy = admission_policy("materials.upload_video_url")
    budget = material_types.RemoteCallBudget(deadline, hard, policy.lease_ms)

    def check_current() -> None:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            dist = _load_distribution(db, context, distribution_id)
            material = load_material(
                db,
                context=context,
                bc_id=dist.bc_id,
                material_id=dist.material_id,
                lock=True,
            )
            operation = _locked_operation(db, context, operation_id)
            if operation.attempt_token != claim or dist.operation_id != operation_id:
                raise DomainError("material_claim_changed", "素材操作已由其他任务接管")
            _require_relay(
                db,
                context=context,
                material=material,
                operation=operation,
                source_route=load_material_route(
                    dist.source_route, context=context, bc_id=dist.bc_id
                ),
            )
            _target_access(db, context, dist, upload=True)
            budget.timeout(upload=True)

    check_current()
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
        before_request=check_current,
    ) as gateway:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            dist = _load_distribution(db, context, distribution_id)
            _locked_material(db, context, dist.material_id)
            operation = _locked_operation(db, context, operation_id)
            if operation.attempt_token != claim or dist.operation_id != operation_id:
                return None
            _target_access(db, context, dist, upload=True)
            operation.status, dist.status = "sending", "preparing"
            operation.remote_response = {
                **operation.remote_response,
                "send_armed": True,
                "upload_connection_id": str(route.connection_id),
            }
        try:
            receipt = gateway.materials.upload_video_url(
                material_types.URLVideoUpload(
                    work["advertiser_id"],
                    preview.url,
                    work["remote_name"],
                    work["content_md5"],
                    work["byte_size"],
                ),
                budget=budget,
            )
        except Exception as error:
            if isinstance(error, api.SdkAdmissionDeferred) or (
                isinstance(error, RemoteCallError) and error.effect == "NOT_SENT"
            ):
                with Session(database_engine) as db, db.begin():
                    dist = _load_distribution(db, context, distribution_id)
                    _locked_material(db, context, dist.material_id)
                    operation = _locked_operation(db, context, operation_id)
                    if (
                        operation.attempt_token == claim
                        and dist.operation_id == operation_id
                    ):
                        operation.remote_response = {
                            **operation.remote_response,
                            "send_armed": False,
                        }
            raise
        evidence = api.receipt_evidence(receipt)
        for receipt_attempt in range(2):
            try:
                _relay_receipt(
                    database_engine,
                    context=context,
                    distribution_id=distribution_id,
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

    return evidence


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
                        route=load_material_route(
                            dist.target_route, context=context, bc_id=dist.bc_id
                        ),
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
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
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
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
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
                    route=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ),
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
                route=load_material_route(
                    dist.target_route, context=context, bc_id=dist.bc_id
                ),
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
                if operation.path == "share_source":
                    _require_relay(
                        session,
                        context=context,
                        material=material,
                        operation=operation,
                        source_route=load_material_route(
                            dist.source_route, context=context, bc_id=dist.bc_id
                        ),
                    )
                else:
                    require_upload_path(
                        session,
                        context=context,
                        material=material,
                        advertiser_id=dist.advertiser_id,
                        route=load_material_route(
                            dist.target_route, context=context, bc_id=dist.bc_id
                        ),
                    )
            else:
                if not material.video_md5:
                    raise DomainError(
                        "material_digest_missing", "素材缺少可核实内容摘要"
                    )
                require_execution_config(
                    upload=False,
                    endpoint="materials.get_videos"
                    if operation.remote_response.get("video_id")
                    else "materials.search_videos",
                    channel=load_material_route(
                        dist.target_route, context=context, bc_id=dist.bc_id
                    ).channel,
                )
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
            "target_route": dist.target_route,
            "source_route": dist.source_route,
            "material_id": material.id,
            "advertiser_id": dist.advertiser_id,
            "remote_name": operation.remote_response.get("remote_name")
            or remote_name(material),
            "byte_size": material.byte_size,
            "strict_video": material.current_object_generation is not None
            or operation.remote_response.get("transport") == "url_relay",
        }
        content_md5 = material.video_md5 or ""
    sent = False
    evidence: dict[str, str] | tuple[list[dict[str, str]], bool] | None = None
    original_scope: AbstractContextManager[OriginalFile | None]
    try:
        if kind == "prepare" and work.get("transport") == "url_relay":
            evidence = _send_relay(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                distribution_id=distribution_id,
                operation_id=operation_id,
                claim=claim,
                work=work,
                deadline=deadline,
                hard=hard,
            )
            if evidence is None:
                return
        else:
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
                route = load_material_route(
                    work["target_route"], context=context, bc_id=work["bc_id"]
                )
                logical = (
                    "materials.upload_video_file"
                    if original
                    else "materials.get_videos"
                    if work.get("video_id")
                    else "materials.search_videos"
                )
                policy = admission_policy(logical)
                budget = material_types.RemoteCallBudget(
                    deadline, hard, policy.lease_ms
                )
                require_execution_config(
                    upload=kind == "prepare", endpoint=logical, channel=route.channel
                )

                def check_current() -> None:
                    with (
                        bounded_session(database_engine, task_deadline=deadline) as db,
                        db.begin(),
                    ):
                        dist = _load_distribution(db, context, distribution_id)
                        load_material(
                            db,
                            context=context,
                            bc_id=dist.bc_id,
                            material_id=dist.material_id,
                            lock=True,
                        )
                        operation = _locked_operation(db, context, operation_id)
                        if (
                            operation.attempt_token != claim
                            or dist.operation_id != operation_id
                        ):
                            raise DomainError(
                                "material_claim_changed", "素材操作已由其他任务接管"
                            )
                        _target_access(
                            db,
                            context,
                            dist,
                            upload=kind == "prepare",
                            connection_id=_work_connection(work),
                        )
                        budget.timeout(upload=kind == "prepare")

                check_current()
                with open_tiktok_gateway(
                    database_engine=database_engine,
                    redis_client=redis_client,
                    context=context,
                    route=route,
                    task_deadline=deadline,
                    before_request=check_current,
                ) as gateway:
                    if original:
                        with (
                            bounded_session(
                                database_engine, task_deadline=deadline
                            ) as db,
                            db.begin(),
                        ):
                            dist = _load_distribution(db, context, distribution_id)
                            current_material = load_material(
                                db,
                                context=context,
                                bc_id=dist.bc_id,
                                material_id=dist.material_id,
                                lock=True,
                            )
                            operation = _locked_operation(db, context, operation_id)
                            if (
                                operation.attempt_token != claim
                                or dist.operation_id != operation_id
                            ):
                                return
                            _target_access(db, context, dist, upload=True)
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
                                "upload_connection_id": str(route.connection_id),
                            }
                        sent = True
                        receipt = gateway.materials.upload_video_file(
                            material_types.FileVideoUpload(
                                work["advertiser_id"],
                                original.path,
                                work["remote_name"],
                                original.md5,
                                work["byte_size"],
                            ),
                            budget=budget,
                        )
                        evidence = api.receipt_evidence(receipt)
                        _relay_receipt(
                            database_engine,
                            context=context,
                            distribution_id=distribution_id,
                            operation_id=operation_id,
                            claim=claim,
                            evidence=evidence,
                        )
                    elif work.get("video_id"):
                        record = gateway.materials.read_video(
                            advertiser_id=work["advertiser_id"],
                            video_id=work["video_id"],
                            budget=budget,
                        )
                        evidence = api.verified_video(
                            {"list": [api.video_record_data(record)] if record else []},
                            md5=content_md5,
                            expected_video_id=work["video_id"],
                            expected_size=work["byte_size"]
                            if work["strict_video"]
                            else None,
                        )
                    else:
                        page = gateway.materials.search_videos(
                            advertiser_id=work["advertiser_id"],
                            page=work.get("search_page", 1),
                            material_ids=(),
                            budget=budget,
                        )
                        if work.get("transport") == "url_relay":
                            total = page.total_pages
                            if not 0 <= total <= 100 or not 1 <= page.page <= 100:
                                raise DomainError(
                                    "material_reconciliation_bounded",
                                    "目标核查超过有界分页范围",
                                )
                            work["observed_search_total"] = total
                            work["search_changed"] = work.get("search_total") not in (
                                None,
                                total,
                            )
                        evidence = api.search_page(
                            api.video_page_data(page),
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
                if operation.remote_response.get("conflicting_video_id"):
                    raise DomainError(
                        "material_reconciliation_ambiguous", "实际目标回执存在歧义"
                    )
                operation.remote_response = {
                    **operation.remote_response,
                    **evidence,
                    "upload_video_id": evidence["video_id"],
                    "upload_mid": evidence.get("mid"),
                }
                operation.status, dist.status = "verifying", "verifying"
            elif work.get("video_id") and evidence:
                assert isinstance(evidence, dict)
                if operation.remote_response.get("conflicting_video_id"):
                    raise DomainError(
                        "material_reconciliation_ambiguous", "实际目标回执存在歧义"
                    )
                _publish_mapping(
                    session,
                    context,
                    dist,
                    evidence,
                    connection_id=_work_connection(work),
                )
                operation.remote_response = {**operation.remote_response, **evidence}
                operation.status, dist.status = "succeeded", "ready"
            elif not work.get("video_id"):
                assert isinstance(evidence, tuple)
                matches, last = evidence
                candidates = {
                    item["video_id"]: item for item in work.get("candidates", [])
                }
                candidates.update({item["video_id"]: item for item in matches})
                if work.get("transport") == "url_relay":
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_total": work["observed_search_total"],
                    }
                if work.get("search_changed"):
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1,
                        "search_total": None,
                        "candidates": [],
                        "error_code": "material_reconciliation_incomplete",
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
                elif len(candidates) > 1:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1,
                        "candidates": list(candidates.values())[:2]
                        if work.get("transport") == "url_relay"
                        else [],
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
    except SDK_SCOPE_INTERRUPTS:
        raise
    except Exception as error:
        if isinstance(error, api.SdkAdmissionDeferred) or (
            isinstance(error, RemoteCallError) and error.effect == "NOT_SENT"
        ):
            sent = False
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
                if sent
                or operation.remote_response.get("send_armed")
                or kind == "verify"
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
