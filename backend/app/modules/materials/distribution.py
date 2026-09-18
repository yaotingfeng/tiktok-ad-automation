"""Submission-only target preparation with independently scoped source and target."""

from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict
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
from .batch_validation import BatchSourceVerifier
from .channel_policy import require_url_upload
from .distribution_sources import distribution_sources
from .file_names import video_file_name
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from .readiness import (
    get_material_readiness,
    get_material_readiness_batch,
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
from .response_archive import material_response_observer
from .routes import (
    load_material_route,
    require_material_route,
    require_same_route,
)
from .schemas import AssetPreparation, MaterialReadiness
from .sharing import distribution_transport
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
RECONCILIATION_MAX_CLAIMS = 120
RECONCILIATION_SECONDS = 15 * 60
RECONCILIATION_MAX_NO_PROGRESS = 3


def _reconciliation_exhausted(operation: MaterialAssetOperation) -> bool:
    budget = operation.remote_response.get("reconciliation_budget")
    if not budget:
        return False
    return (
        budget["claims"] >= RECONCILIATION_MAX_CLAIMS
        or budget["no_progress"] >= RECONCILIATION_MAX_NO_PROGRESS
        or datetime.now(UTC) - datetime.fromisoformat(budget["started_at"])
        >= timedelta(seconds=RECONCILIATION_SECONDS)
    )


def _stop_reconciliation(
    dist: MaterialDistribution, operation: MaterialAssetOperation
) -> None:
    # 停止自动读取不是未上传证明；原请求、回执及内容身份全部保留。
    operation.remote_response = {
        **operation.remote_response,
        "reconciliation_stopped": True,
        "error_code": "material_reconciliation_budget_exhausted",
    }
    dist.status = operation.status = "result_unknown"
    dist.reason_code = "material_reconciliation_budget_exhausted"
    operation.attempt_token, operation.claimed_until = None, None


def _claim_reconciliation(operation: MaterialAssetOperation) -> None:
    budget: dict[str, Any] = operation.remote_response.get("reconciliation_budget") or {
        "started_at": datetime.now(UTC).isoformat(),
        "claims": 0,
        "no_progress": 0,
    }
    operation.remote_response = {
        **operation.remote_response,
        "reconciliation_budget": {**budget, "claims": budget["claims"] + 1},
    }


def _reconciliation_progress(
    operation: MaterialAssetOperation, *, progress: bool
) -> None:
    budget = operation.remote_response["reconciliation_budget"]
    response = {
        **operation.remote_response,
        "reconciliation_budget": {
            **budget,
            "no_progress": 0 if progress else budget["no_progress"] + 1,
        },
    }
    if progress:
        response.pop("error_code", None)
    operation.remote_response = response


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
    # 显式接替后旧未知只保留历史，不再安排核实或准备消息。
    if dist.superseded_by_id is not None or operation.superseded_by_id is not None:
        return
    if read_only and kind != "verify":
        raise DomainError("invalid_asset_task", "只读核实不能安排上传")
    if operation.remote_response.get(
        "reconciliation_complete"
    ) or operation.remote_response.get("reconciliation_stopped"):
        if not (observe and read_only):
            return
        # 完整空查/预算耗尽不是未发送证明。仅显式只读核查重开已结束轮次；
        # 进行中的重复点击不刷新预算，自动补偿和旧消息不能续命或重新上传。
        operation.remote_response = {
            **operation.remote_response,
            "reconciliation_complete": False,
            "reconciliation_stopped": False,
            "search_page": 1,
            "search_total": None,
            "candidates": [],
            "revision": operation.remote_response.get("revision", 0) + 1,
        }
        operation.remote_response = {
            key: value
            for key, value in operation.remote_response.items()
            if key not in {"reconciliation_budget", "error_code"}
        }
    elif observe and read_only and operation.status == "succeeded":
        # 成功回执的再次复核沿用既有消息身份；旧轮预算不能阻止新的显式读取。
        operation.remote_response = {
            key: value
            for key, value in operation.remote_response.items()
            if key not in {"reconciliation_budget", "error_code"}
        }
    if (
        kind == "verify"
        and operation.attempt_token is None
        and _reconciliation_exhausted(operation)
    ):
        _stop_reconciliation(dist, operation)
        return
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
    source: AccountMaterial | None = None,
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
            source = source or distribution_sources(
                session,
                context=context,
                materials=[material],
                advertiser_id=advertiser_id,
                route=route,
            ).get(material_id)
            if source is None:
                raise DomainError(
                    "material_remote_source_unavailable", "请恢复来源授权或补传原件"
                )
            operation.remote_response = {
                "transport": distribution_transport(
                    source_bc_id=source.bc_id, target_bc_id=bc_id
                ),
                "source_bc_id": source.bc_id,
                "source_material_id": str(source.material_id),
                "source_asset_id": str(source.id),
                "source_advertiser_id": source.advertiser_id,
                "source_connection_id": str(source.connection_id),
                "source_video_id": source.video_id,
                "remote_name": video_file_name(
                    material.file_name, correlation=operation.id.hex[:8]
                ),
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
    _seed_owner: bool = False,
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
    material = load_material(
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
    return _ensure_target_asset(
        session,
        context=context,
        bc_id=bc_id,
        material=material,
        advertiser_id=advertiser_id,
        route=route,
        readiness=readiness,
        seed_owner=_seed_owner,
    )


def ensure_target_assets(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_ids: list[UUID],
    advertiser_id: str,
    route: FrozenTikTokRoute,
) -> dict[UUID, AssetPreparation]:
    """同一短事务准备一个账户的至多20条素材，复用单条准备实现与批读权限。"""
    if not 1 <= len(material_ids) <= 20 or len(set(material_ids)) != len(material_ids):
        raise ValueError("invalid material preparation slice")
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
    materials = session.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_(material_ids),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
    ).all()
    if len(materials) != len(material_ids):
        raise DomainError("material_not_found", "素材切片范围不完整")
    readiness = get_material_readiness_batch(
        session,
        context=context,
        bc_id=bc_id,
        material_ids=material_ids,
        advertiser_id=advertiser_id,
        route=route,
    )
    return {
        material.id: _ensure_target_asset(
            session,
            context=context,
            bc_id=bc_id,
            material=material,
            advertiser_id=advertiser_id,
            route=route,
            readiness=readiness[material.id],
            seed_owner=False,
        )
        for material in materials
    }


def _ensure_target_asset(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material: MaterialFile,
    advertiser_id: str,
    route: FrozenTikTokRoute,
    readiness: MaterialReadiness,
    seed_owner: bool,
) -> AssetPreparation:
    # 两个入口均已在当前事务核验权限并锁定素材；后续选源/去重/seed只有一份。
    material_id = material.id
    if (
        readiness.path == "existing_target"
        and readiness.mapping is not None
        and readiness.mapping.material_id != material_id
    ):
        from .repository import asset_public

        # 同内容别名仅建立消费引用；实际 VID、图片和验证时间来自同目标账户。
        actual = session.get(AccountMaterial, readiness.mapping.asset_id)
        assert actual
        values = actual.model_dump()
        values.update(id=uuid4(), material_id=material_id)
        alias = AccountMaterial(**values)
        session.add(alias)
        session.flush()
        if mapping_fresh(alias):
            return AssetPreparation(state="ready", mapping=asset_public(alias))
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
            MaterialDistribution.bc_id == bc_id,
            MaterialDistribution.material_id == material_id,
            MaterialDistribution.advertiser_id == advertiser_id,
            col(MaterialDistribution.superseded_by_id).is_(None),
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
    if not seed_owner and readiness.path == "share_source":
        candidate = distribution_sources(
            session,
            context=context,
            materials=[material],
            advertiser_id=advertiser_id,
            route=route,
        ).get(material_id)
        if candidate is not None and candidate.bc_id != bc_id:
            from .bc_seeding import ensure_bc_seed

            seed = ensure_bc_seed(
                session, context=context, material=material, route=route
            )
            if seed.advertiser_id == advertiser_id and seed.material_id == material_id:
                return AssetPreparation(state="queued", task_id=seed.distribution_id)
            operation = reserve_asset_operation(
                session,
                context=context,
                material_id=material_id,
                advertiser_id=advertiser_id,
                path="share_source",
                action="build",
                route=route,
            )
            dist = MaterialDistribution(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                material_id=material_id,
                advertiser_id=advertiser_id,
                actor_id=context.actor_id,
                operation_id=operation.id,
                seed_id=seed.id,
                source_bc_id=bc_id,
                source_material_id=seed.material_id,
                source_route=route.model_dump(mode="json"),
                target_route=route.model_dump(mode="json"),
                path="share_source",
            )
            session.add(dist)
            session.flush()
            queue_distribution(session, dist, operation, kind="prepare")
            return AssetPreparation(state="queued", task_id=dist.id)
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
            bc_id=operation.remote_response["source_bc_id"],
            material_id=UUID(operation.remote_response["source_material_id"]),
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
                bc_id=source.bc_id,
                connection_id=source.connection_id,
            )
            if previous_dependency
            else freeze_route(
                session,
                context=context,
                bc_id=source.bc_id,
                connection_id=source.connection_id,
            )
        )
        require_material_route(
            session,
            context=context,
            route=source_route,
            bc_id=source.bc_id,
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
        source_bc_id=operation.remote_response.get("source_bc_id"),
        source_material_id=UUID(operation.remote_response["source_material_id"])
        if source_asset_id
        else None,
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


def _delivery_matches(
    operation: MaterialAssetOperation,
    *,
    operation_id: UUID | None,
    revision: int | None,
    recovery_claim_id: UUID | None,
) -> bool:
    return (
        operation.superseded_by_id is None
        and not operation.remote_response.get("reconciliation_complete")
        and not operation.remote_response.get("reconciliation_stopped")
        and (operation_id is None or operation.id == operation_id)
        and (
            revision is None or revision == operation.remote_response.get("revision", 0)
        )
        and (recovery_claim_id is None or operation.attempt_token == recovery_claim_id)
    )


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
    # 迟到旧回执不能覆盖新代 VID 或清空新封面；响应正文仍独立留档。
    if dist.superseded_by_id is not None:
        return
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
    from .bc_seeding import wake_seed_dependents

    wake_seed_dependents(session, context, dist)


def _blocked(dist: MaterialDistribution, code: str) -> None:
    dist.status, dist.reason_code = "blocked", code


def _invalidate_mapping(
    session: Session,
    context: TenantContext,
    dist: MaterialDistribution,
    work: dict[str, Any],
) -> None:
    if dist.superseded_by_id is not None:
        return
    # 真实详情不再支持正证据时，保留同一 VID 进入只读核查；不影响新身份或新连接。
    mapping = target_mapping(
        session,
        context=context,
        bc_id=dist.bc_id,
        material_id=dist.material_id,
        advertiser_id=dist.advertiser_id,
    )
    if (
        mapping is not None
        and mapping.video_id == work.get("video_id")
        and mapping.connection_id == _work_connection(work)
    ):
        mapping.status = "result_unknown"


def _work_connection(work: dict[str, Any]) -> UUID:
    return FrozenTikTokRoute.model_validate(work["target_route"]).connection_id


def _require_distribution_source(
    session: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    operation: MaterialAssetOperation,
    source_route: FrozenTikTokRoute,
    verifier: BatchSourceVerifier | None = None,
) -> None:
    response = operation.remote_response
    target_route = load_material_route(
        operation.frozen_route, context=context, bc_id=operation.bc_id
    )
    resolve_source = verifier.source if verifier is not None else resolve_remote_source
    require_route = (
        verifier.require_route if verifier is not None else require_material_route
    )
    source = resolve_source(
        session,
        context=context,
        bc_id=source_route.bc_id,
        material_id=UUID(response.get("source_material_id", str(material.id))),
        source_asset_id=UUID(response["source_asset_id"]),
    )
    source_file = (
        verifier.materials.get(source.material_id)
        if source is not None and verifier is not None
        else session.get(MaterialFile, source.material_id)
        if source
        else None
    )
    if (
        source is None
        or source_file is None
        or (source.advertiser_id, str(source.connection_id), source.video_id)
        != (
            response.get("source_advertiser_id"),
            response.get("source_connection_id"),
            response.get("source_video_id"),
        )
        or response.get("content_md5") != material.video_md5
        or (source_file.video_md5, source_file.byte_size)
        != (material.video_md5, material.byte_size)
    ):
        raise DomainError("material_remote_source_unavailable", "来源证据或权限已改变")
    transport = distribution_transport(
        source_bc_id=source.bc_id, target_bc_id=operation.bc_id
    )
    if response.get("transport") != transport:
        raise DomainError(
            "material_distribution_route_changed",
            "分发方式已变化，请重新准备尚未发送的任务",
        )
    if source.material_id != material.id and (
        not material.sha256
        or not material.digest_verified_at
        or not source_file.digest_verified_at
        or source_file.sha256 != material.sha256
    ):
        raise DomainError("material_source_digest_changed", "来源内容身份已变化")
    require_route(
        session,
        context=context,
        route=source_route,
        bc_id=source.bc_id,
        advertiser_id=source.advertiser_id,
        capability="read",
    )
    if source_route.connection_id != source.connection_id:
        raise DomainError("frozen_route_changed", "来源连接已改变")
    if transport == "native_share":
        # 共享接口使用目标通道的一份授权；该授权必须同时能操作源、目标账户。
        require_route(
            session,
            context=context,
            route=target_route,
            bc_id=source.bc_id,
            advertiser_id=source.advertiser_id,
            capability="upload",
        )
        require_execution_config(
            upload=True,
            endpoint="materials.share_assets",
            original=False,
            channel=target_route.channel,
        )
        return
    require_remote_material(material)
    if material.byte_size > settings.MATERIAL_URL_MAX_UPLOAD_BYTES:
        raise DomainError(
            "url_upload_capacity_exceeded", "素材超过当前URL转存工程容量限制"
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
        # 网关回执可迟于显式接替到达；完整正文由 observer 留档，旧业务证据不可改写。
        if dist.superseded_by_id is not None or operation.superseded_by_id is not None:
            return
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


def _send_remote_asset(
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
    route = load_material_route(
        work["target_route"], context=context, bc_id=work["bc_id"]
    )
    native = work["transport"] == "native_share"
    preview = None
    if native:
        # 原生共享不传输视频字节；源读取和共享共用一个短期限会话，避免重复握手。
        # 仍按每个操作独立准入，不能让读取借用上传的长预算或跨任务缓存会话。
        deadline = min(
            deadline, datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
        )
    else:
        preview = read_frozen_remote_source(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            bc_id=work["source_bc_id"],
            material_id=UUID(work["source_material_id"]),
            source_asset_id=UUID(work["source_asset_id"]),
            route=load_material_route(
                work["source_route"], context=context, bc_id=work["source_bc_id"]
            ),
            deadline=deadline,
        )
    policy = admission_policy(
        "materials.share_assets" if native else "materials.upload_video_url"
    )
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
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                raise DomainError("material_claim_changed", "素材操作已由其他任务接管")
            _require_distribution_source(
                db,
                context=context,
                material=material,
                operation=operation,
                source_route=load_material_route(
                    dist.source_route,
                    context=context,
                    bc_id=operation.remote_response.get("source_bc_id", dist.bc_id),
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
        # 原生共享会读取源账户；只有 URL 转存会话完全属于本次目标操作。
        response_observer=None
        if native
        else material_response_observer(
            database_engine=database_engine,
            context=context,
            route=route,
            material_id=work["material_id"],
            operation_id=operation_id,
            advertiser_id=work["advertiser_id"],
            task_deadline=deadline,
        ),
    ) as gateway:
        if native:
            # 共享使用 MID，且平台复制源名称；保存真实名称才能恢复重名/超时结果。
            record = gateway.materials.read_video(
                advertiser_id=work["source_advertiser_id"],
                video_id=work["source_video_id"],
                budget=material_types.RemoteCallBudget(
                    deadline,
                    READ_HARD_LIMIT,
                    admission_policy("materials.get_videos").lease_ms,
                ),
            )
            if (
                record is None
                or record.video_id != work["source_video_id"]
                or not record.mid
                or not record.file_name
                or record.md5 != work["content_md5"]
            ):
                raise DomainError(
                    "material_share_source_missing", "来源缺少共享 MID、名称或内容摘要"
                )
            work["source_mid"], work["remote_name"] = record.mid, record.file_name
            with (
                bounded_session(database_engine, task_deadline=deadline) as db,
                db.begin(),
            ):
                dist = _load_distribution(db, context, distribution_id)
                _locked_material(db, context, dist.material_id)
                operation = _locked_operation(db, context, operation_id)
                if (
                    operation.superseded_by_id is not None
                    or dist.superseded_by_id is not None
                    or operation.attempt_token != claim
                    or dist.operation_id != operation_id
                ):
                    return None
                operation.remote_response = {
                    **operation.remote_response,
                    "source_mid": record.mid,
                    "remote_name": record.file_name,
                }
            check_current()
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            dist = _load_distribution(db, context, distribution_id)
            _locked_material(db, context, dist.material_id)
            operation = _locked_operation(db, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return None
            _target_access(db, context, dist, upload=True)
            operation.status, dist.status = "sending", "preparing"
            operation.remote_response = {
                **operation.remote_response,
                "send_armed": True,
                "upload_connection_id": str(route.connection_id),
            }
        try:
            if native:
                share_receipt = gateway.materials.share_assets(
                    material_types.AssetShare(
                        advertiser_id=work["source_advertiser_id"],
                        material_ids=(work["source_mid"],),
                        shared_advertiser_ids=(work["advertiser_id"],),
                    ),
                    budget=budget,
                )
                with Session(database_engine) as db, db.begin():
                    dist = _load_distribution(db, context, distribution_id)
                    _locked_material(db, context, dist.material_id)
                    operation = _locked_operation(db, context, operation_id)
                    if (
                        operation.superseded_by_id is not None
                        or dist.superseded_by_id is not None
                        or operation.attempt_token != claim
                        or dist.operation_id != operation_id
                    ):
                        return None
                    rejected = work["source_mid"] in share_receipt.failed_infos.get(
                        work["advertiser_id"], ()
                    )
                    operation.remote_response = {
                        **operation.remote_response,
                        "share_receipt": asdict(share_receipt),
                        "share_acknowledged": not rejected,
                    }
                    if rejected:
                        # 平台已明确此目标/MID 失败，不能当作待核实成功或转为重传。
                        operation.status, dist.status = "failed", "blocked"
                        operation.remote_response = {
                            **operation.remote_response,
                            "definite_no_effect": True,
                            "error_code": "material_share_failed",
                        }
                        operation.attempt_token = operation.claimed_until = None
                        dist.reason_code = "material_share_failed"
                        return None
                return {}
            assert preview is not None
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
    """一次持久发送；来源和恢复读取各自遵守工作进程期限与准入。"""
    if kind not in {"prepare", "verify"} or (read_only and kind != "verify"):
        raise DomainError("invalid_asset_task", "目标素材工作任务无效")
    # 旧队列消息必须在授权查询、素材锁及恢复分支之前退出；否则来源已完成
    # 而缓存过期时，旧观察消息会错误建立新一代读取并持续放大队列。
    with Session(database_engine) as session:
        current = _load_distribution(session, context, distribution_id)
        if current.superseded_by_id is not None:
            return
        if current.status not in ACTIVE_DISTRIBUTIONS and not (
            read_only and current.status in {"ready", "blocked"}
        ):
            return
        pending = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.id == current.operation_id,
            )
        ).one_or_none()
        if pending is None or not _delivery_matches(
            pending,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
    if kind == "prepare" and not read_only:
        from .bc_seeding import resume_ready_seed_dependents, resume_seed_dependency

        resume_ready_seed_dependents(
            database_engine=database_engine,
            context=context,
            distribution_id=distribution_id,
        )

        with Session(database_engine) as session, session.begin():
            dependency = _load_distribution(session, context, distribution_id)
            if (
                dependency.seed_id is not None
                and dependency.status in ACTIVE_DISTRIBUTIONS
            ):
                _locked_material(session, context, dependency.material_id)
                assert dependency.operation_id is not None
                pending = _locked_operation(session, context, dependency.operation_id)
                if operation_id is not None and pending.id != operation_id:
                    return
                if revision is not None and revision != pending.remote_response.get(
                    "revision", 0
                ):
                    return
                try:
                    if resume_seed_dependency(
                        session, context=context, dist=dependency, operation=pending
                    ):
                        return
                except DomainError as error:
                    dependency.status, dependency.reason_code = "blocked", error.code
                    pending.status = "failed"
                    pending.remote_response = {
                        **pending.remote_response,
                        "definite_no_effect": True,
                        "error_code": error.code,
                    }
                    return
    if kind == "prepare":
        from .batch_distribution import try_prepare_batch

        if try_prepare_batch(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=distribution_id,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
    elif not read_only:
        from .batch_verification import try_verify_batch

        if try_verify_batch(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=distribution_id,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
        ):
            return
    claim = uuid4()
    hard = UPLOAD_HARD_LIMIT if kind == "prepare" else READ_HARD_LIMIT
    lease = UPLOAD_CLAIM_SECONDS if kind == "prepare" else READ_CLAIM_SECONDS
    deadline = datetime.now(UTC) + timedelta(seconds=hard - 5)
    with Session(database_engine) as session, session.begin():
        dist = _load_distribution(session, context, distribution_id)
        if dist.superseded_by_id is not None:
            return
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
            # 获取素材锁前可能已有授权接替提交；刷新旧分发，不能用陈旧身份继续。
            session.refresh(dist)
            if dist.superseded_by_id is not None:
                return
            _target_access(session, context, dist, upload=False)
        except DomainError as error:
            if dist.operation_id is not None:
                # 目标撤权/绑定换代发生在发送前时，释放明确未发送的旧操作；
                # 新提交可冻结新授权，已 armed 或在途操作继续保留原核实身份。
                # 租户权限可能先于 load_material 失败，记账仍遵守素材→操作锁序。
                _locked_material(session, context, dist.material_id)
                session.refresh(dist)
                operation = _locked_operation(session, context, dist.operation_id)
                # 准备和核实的拒权分支都必须受当前头约束，迟到旧消息不能改历史状态。
                if (
                    dist.superseded_by_id is not None
                    or operation.superseded_by_id is not None
                ):
                    return
                if (
                    kind == "prepare"
                    and not read_only
                    and (operation_id is None or operation_id == operation.id)
                    and (
                        revision is None
                        or revision == operation.remote_response.get("revision", 0)
                    )
                    and operation.status == "pending"
                    and operation.attempt_token is None
                    and operation.claimed_until is None
                    and not operation.remote_response.get("send_armed")
                    and _attempt(session, operation.id) is None
                ):
                    operation.status = "failed"
                    operation.remote_response = {
                        **operation.remote_response,
                        "definite_no_effect": True,
                        "error_code": error.code,
                    }
            _blocked(dist, error.code)
            return
        assert dist.operation_id
        if operation_id is not None and operation_id != dist.operation_id:
            return
        operation = _locked_operation(session, context, dist.operation_id)
        # 快速检查后可能发生并发换代，持锁后再次核对，且早于所有来源分支。
        if not _delivery_matches(
            operation,
            operation_id=operation_id,
            revision=revision,
            recovery_claim_id=recovery_claim_id,
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
        if operation.claimed_until and operation.claimed_until > datetime.now(UTC):
            return
        if kind == "verify" and _reconciliation_exhausted(operation):
            _stop_reconciliation(dist, operation)
            return
        expired_send = operation.status == "sending"
        if expired_send:
            operation.status, dist.status = "result_unknown", "result_unknown"
        if operation.status == "failed":
            if operation.path == "share_source":
                _blocked(
                    dist,
                    operation.remote_response.get(
                        "error_code", "material_share_failed"
                    ),
                )
                return
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
                    _require_distribution_source(
                        session,
                        context=context,
                        material=material,
                        operation=operation,
                        source_route=load_material_route(
                            dist.source_route,
                            context=context,
                            bc_id=operation.remote_response.get(
                                "source_bc_id", dist.bc_id
                            ),
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
        if kind == "verify":
            _claim_reconciliation(operation)
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
            or operation.remote_response.get("transport") == "url_relay"
            or operation.remote_response.get("batch_discovered", False),
        }
        content_md5 = material.video_md5 or ""
    sent = False
    evidence: dict[str, str] | tuple[list[dict[str, str]], bool] | None = None
    original_scope: AbstractContextManager[OriginalFile | None]
    try:
        if kind == "prepare" and work.get("transport") in {"native_share", "url_relay"}:
            evidence = _send_remote_asset(
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
                            operation.superseded_by_id is not None
                            or dist.superseded_by_id is not None
                            or operation.attempt_token != claim
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
                    # 目标详情与搜索分别留档，不能把核验响应当作原上传回执。
                    response_observer=material_response_observer(
                        database_engine=database_engine,
                        context=context,
                        route=route,
                        material_id=work["material_id"],
                        operation_id=operation_id,
                        advertiser_id=work["advertiser_id"],
                        task_deadline=deadline,
                    ),
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
                                operation.superseded_by_id is not None
                                or dist.superseded_by_id is not None
                                or operation.attempt_token != claim
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
                            video_name=work["remote_name"]
                            if work.get("transport") == "native_share"
                            else None,
                            advertiser_id=work["advertiser_id"],
                            page=work.get("search_page", 1),
                            material_ids=(),
                            budget=budget,
                        )
                        if work.get("transport") in {"native_share", "url_relay"}:
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
                        # 按名称查询的一页完整唯一结果已携带可用状态和摘要时，直接使用
                        # 这份真实目标证据；不再为相同 VID 另排一次详情请求。
                        if (
                            work.get("transport") == "native_share"
                            and page.page == 1
                            and page.total_pages == 1
                        ):
                            matches, last = evidence
                            if last and len(matches) == 1:
                                match_id = matches[0]["video_id"]
                                record = next(
                                    (
                                        row
                                        for row in page.rows
                                        if row.video_id == match_id
                                    ),
                                    None,
                                )
                                if record is not None:
                                    work["verified_search_match"] = api.verified_video(
                                        {"list": [api.video_record_data(record)]},
                                        md5=content_md5,
                                        expected_video_id=match_id,
                                        expected_size=work["byte_size"]
                                        if work["strict_video"]
                                        else None,
                                    )
        with Session(database_engine) as session, session.begin():
            dist = _load_distribution(session, context, distribution_id)
            _locked_material(session, context, dist.material_id)
            operation = _locked_operation(session, context, operation_id)
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return
            if kind == "prepare":
                assert isinstance(evidence, dict)
                if work.get("transport") == "native_share":
                    operation.remote_response = {
                        **operation.remote_response,
                        "share_acknowledged": True,
                    }
                    # 共享 ACK 没有目标 VID，仍须发现目标库存的真实身份。
                    operation.status, dist.status = "verifying", "verifying"
                else:
                    if operation.remote_response.get("conflicting_video_id"):
                        raise DomainError(
                            "material_reconciliation_ambiguous", "实际目标回执存在歧义"
                        )
                    operation.remote_response = {
                        **operation.remote_response,
                        **evidence,
                        "upload_video_id": evidence["video_id"],
                        "upload_mid": evidence.get("mid"),
                        "confirmation_source": "upload_receipt",
                    }
                    # 成功上传回执与来源 URL 导入使用同一语义；不伪造摘要或可用状态。
                    _publish_mapping(
                        session,
                        context,
                        dist,
                        evidence,
                        connection_id=_work_connection(work),
                    )
                    operation.status, dist.status = "succeeded", "ready"
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
                if work.get("transport") == "url_relay":
                    # 严格名称/摘要搜索后的 VID 详情也能确认实际 URL 上传结果；
                    # 独立记录核实证据，不伪造可能已经丢失的原上传响应。
                    operation.remote_response = {
                        **operation.remote_response,
                        "verified_upload_video_id": evidence["video_id"],
                    }
                operation.status, dist.status = "succeeded", "ready"
            elif not work.get("video_id"):
                assert isinstance(evidence, tuple)
                matches, last = evidence
                candidates = {
                    item["video_id"]: item for item in work.get("candidates", [])
                }
                candidates.update({item["video_id"]: item for item in matches})
                if work.get("transport") in {"native_share", "url_relay"}:
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
                    verified = work.get("verified_search_match")
                    if verified and verified["video_id"] in candidates:
                        _publish_mapping(
                            session,
                            context,
                            dist,
                            verified,
                            connection_id=_work_connection(work),
                        )
                        operation.status, dist.status = "succeeded", "ready"
                    else:
                        operation.status, dist.status = "verifying", "verifying"
                else:
                    operation.remote_response = {
                        **operation.remote_response,
                        "search_page": 1 if last else work.get("search_page", 1) + 1,
                        "candidates": [] if last else list(candidates.values()),
                        "reconciliation_complete": last,
                    }
                    operation.status, dist.status = "result_unknown", "result_unknown"
            else:
                operation.status, dist.status = "verifying", "verifying"
                _invalidate_mapping(session, context, dist, work)
            progress = dist.status == "ready" or (
                kind == "verify"
                and not work.get("video_id")
                and not work.get("search_changed")
                and len(candidates) <= 1
            )
            if kind == "verify":
                _reconciliation_progress(operation, progress=progress)
            else:
                operation.remote_response = {
                    key: value
                    for key, value in operation.remote_response.items()
                    if key != "error_code"
                }
            dist.reason_code = (
                None if dist.status == "ready" else "material_result_pending"
            )
            operation.attempt_token, operation.claimed_until = None, None
            from .batch_distribution import sync_batch_member

            sync_batch_member(session, operation, dist)
            if dist.status != "ready":
                queue_distribution(
                    session,
                    dist,
                    operation,
                    read_only=read_only,
                    kind="verify",
                    due=datetime.now(UTC)
                    + timedelta(
                        seconds=0
                        if progress
                        or (
                            work.get("transport") == "native_share"
                            and kind == "prepare"
                        )
                        else 60
                    ),
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
            if (
                operation.superseded_by_id is not None
                or dist.superseded_by_id is not None
                or operation.attempt_token != claim
                or dist.operation_id != operation_id
            ):
                return
            code = (
                error.code
                if isinstance(error, DomainError)
                else "material_response_unknown"
            )
            if kind == "verify" and code == "unsupported_material_schema":
                # 返回非请求 VID 等结构冲突属于已观察到的不一致；传输超时不冒充负证据。
                _invalidate_mapping(session, context, dist, work)
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
            if kind == "verify" and not isinstance(error, api.SdkAdmissionDeferred):
                _reconciliation_progress(operation, progress=False)
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
    from sqlalchemy import String, and_, case, cast, func, or_, text
    from sqlalchemy.dialects.postgresql import UUID as SQLUUID

    from .models import MaterialFile, MaterialUploadAttempt, ObjectUpload

    if type(limit) is not int or not 0 < limit <= 100:
        raise DomainError("invalid_asset_task", "素材恢复批量上限为 100")
    # Worker 硬终止不会立即取消服务器上的长 SQL；数据库自身必须先超时。
    session.connection().execute(text("SET LOCAL statement_timeout = '10s'"))
    session.connection().execute(text("SET LOCAL lock_timeout = '2s'"))
    # 短周期恢复只取 100 条，避免复杂 EXISTS 触发的 JIT 编译挤占本轮预算。
    session.connection().execute(text("SET LOCAL jit = off"))
    now = datetime.now(UTC)
    payload = col(PendingDispatch.payload)

    def payload_uuid(key: str):
        value = payload[key].astext
        # 转换消息引用而非已索引的主键；非法旧消息只是不匹配，不能炸掉整轮。
        return case(
            (
                value.op("~")("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"),
                cast(value, SQLUUID),
            ),
            else_=None,
        )

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
            col(MaterialAssetOperation.superseded_by_id).is_(None),
            payload_uuid("operation_id") == MaterialAssetOperation.id,
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
    from .bc_seeding import seed_dependency_settled

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
            col(MaterialDistribution.superseded_by_id).is_(None),
            col(MaterialAssetOperation.superseded_by_id).is_(None),
            payload_uuid("distribution_id") == MaterialDistribution.id,
            payload_uuid("operation_id") == MaterialAssetOperation.id,
            col(PendingDispatch.task_name).in_(
                ["materials.prepare_target", "materials.verify_target"]
            ),
            remote["reconciliation_complete"].astext.is_distinct_from("true"),
            remote["reconciliation_stopped"].astext.is_distinct_from("true"),
            seed_dependency_settled(),
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
            col(PendingDispatch.task_name).in_(
                [
                    "materials.upload_original",
                    "materials.verify_original",
                    "materials.prepare_target",
                    "materials.verify_target",
                ]
            ),
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
