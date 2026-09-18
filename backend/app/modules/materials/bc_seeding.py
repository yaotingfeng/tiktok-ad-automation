"""同内容在目标 BC 仅转存一次；消费者始终保留自己的目标分发身份。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import groupby
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import local_read_batch
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

from .content_identity import content_key
from .models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from .routes import load_material_route, require_material_route, require_same_route
from .seed_models import MaterialBCSeed
from .source_selection import resolve_primary_account


def seed_dependency_settled():
    """补偿只唤醒已落定的种子，不把等待本身变成每120秒一次的消息。"""
    owner = aliased(MaterialDistribution)
    settled = (
        select(MaterialBCSeed.id)
        .join(owner, col(owner.id) == MaterialBCSeed.distribution_id)
        .where(
            MaterialBCSeed.id == MaterialDistribution.seed_id,
            MaterialBCSeed.tenant_id == MaterialDistribution.tenant_id,
            col(owner.superseded_by_id).is_(None),
            or_(
                col(owner.status).in_(["ready", "blocked"]),
                and_(
                    col(owner.status) == "result_unknown",
                    col(MaterialDistribution.status) != "result_unknown",
                ),
            ),
        )
        .exists()
    )
    return or_(
        col(MaterialDistribution.seed_id).is_(None),
        col(MaterialAssetOperation.remote_response)["transport"].astext.is_not(None),
        settled,
    )


def wake_seed_dependents(
    db: Session, context: TenantContext, owner: MaterialDistribution
) -> None:
    """成功回执与原消费者消息唤醒一起提交，不创建新的轮询代数。"""
    from app.jobs.models import PendingDispatch

    if owner.superseded_by_id is not None:
        return

    consumers = db.exec(
        select(MaterialDistribution, MaterialAssetOperation)
        .join(MaterialBCSeed, col(MaterialBCSeed.id) == MaterialDistribution.seed_id)
        .join(
            MaterialAssetOperation,
            col(MaterialAssetOperation.id) == MaterialDistribution.operation_id,
        )
        .where(
            MaterialBCSeed.tenant_id == context.tenant_id,
            MaterialBCSeed.distribution_id == owner.id,
            col(MaterialDistribution.superseded_by_id).is_(None),
            col(MaterialAssetOperation.superseded_by_id).is_(None),
            MaterialAssetOperation.status == "pending",
            col(MaterialAssetOperation.remote_response)["transport"].astext.is_(None),
        )
        .limit(200)
    ).all()
    keys = [
        f"material-target:{dist.id}:{op.id}:{op.remote_response.get('revision', 0)}:prepare"
        for dist, op in consumers
    ]
    if keys:
        db.exec(
            update(PendingDispatch)
            .where(
                col(PendingDispatch.tenant_id) == context.tenant_id,
                col(PendingDispatch.task_key).in_(keys),
                col(PendingDispatch.published_at).is_not(None),
            )
            .values(published_at=None, available_at=datetime.now(UTC))
        )


def resume_ready_seed_dependents(
    *, database_engine: Any, context: TenantContext, distribution_id: UUID
) -> int:
    """逐账户短事务绑定就绪种子的目标集合，再交给公共原生共享器。

    不在单个素材回执锁内遍历其他素材；锁序与共享批次保持一致。
    已发送、已绑定来源及不同冻结路由的历史消费者不改写。
    """
    from .distribution import _load_distribution
    from .source_uploads import READ_HARD_LIMIT

    deadline = datetime.now(UTC) + timedelta(seconds=READ_HARD_LIMIT - 5)
    with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
        anchor = _load_distribution(db, context, distribution_id)
        if anchor.seed_id is None:
            return 0
        owner = aliased(MaterialDistribution)
        candidates = (
            select(MaterialDistribution, MaterialAssetOperation)
            .join(
                MaterialAssetOperation,
                col(MaterialAssetOperation.id) == MaterialDistribution.operation_id,
            )
            .join(
                MaterialBCSeed, col(MaterialBCSeed.id) == MaterialDistribution.seed_id
            )
            .join(owner, col(owner.id) == MaterialBCSeed.distribution_id)
            .where(
                MaterialDistribution.tenant_id == context.tenant_id,
                col(MaterialDistribution.superseded_by_id).is_(None),
                col(MaterialAssetOperation.superseded_by_id).is_(None),
                col(owner.superseded_by_id).is_(None),
                MaterialDistribution.bc_id == anchor.bc_id,
                MaterialDistribution.actor_id == context.actor_id,
                MaterialDistribution.target_route == anchor.target_route,
                col(MaterialDistribution.status).in_(["queued", "result_unknown"]),
                MaterialAssetOperation.status == "pending",
                col(MaterialAssetOperation.attempt_token).is_(None),
                col(MaterialAssetOperation.remote_response)["transport"].astext.is_(
                    None
                ),
                col(MaterialAssetOperation.remote_response)[
                    "send_armed"
                ].astext.is_distinct_from("true"),
                owner.status == "ready",
            )
        )
        materials = (
            db.connection()
            .execute(
                candidates.with_only_columns(col(MaterialDistribution.material_id))
                .distinct()
                .order_by(col(MaterialDistribution.material_id))
                .limit(20)
            )
            .scalars()
            .all()
        )
        if not materials:
            return 0
        rows = db.exec(
            candidates.where(col(MaterialDistribution.material_id).in_(materials))
            .order_by(
                col(MaterialDistribution.material_id),
                col(MaterialDistribution.advertiser_id),
            )
            .limit(200)
        ).all()
        if not rows:
            # 两次候选读取之间可能已由另一消费者完成，按幂等空操作退出。
            return 0
        # 事务间只传稳定ID与已冻结查询范围，不传ORM对象或权限缓存。
        groups: dict[str, list[UUID]] = {}
        for dist, _ in rows:
            groups.setdefault(dist.advertiser_id, []).append(dist.id)
    changed = 0
    # 200关系不挤进一个5秒资源事务；每账户最多20关系，且共享同一入口期限。
    for advertiser_id in sorted(groups)[:10]:
        with bounded_session(database_engine, task_deadline=deadline) as db, db.begin():
            changed += _resume_ready_seed_account(
                db,
                context=context,
                candidates=candidates,
                identities=groups[advertiser_id],
            )
    return changed


def _resume_ready_seed_account(
    db: Session, *, context: TenantContext, candidates: Any, identities: list[UUID]
) -> int:
    rows = db.exec(candidates.where(col(MaterialDistribution.id).in_(identities))).all()
    if not rows:
        return 0
    # 与共享发送保持素材→操作的统一锁序；一次锁取整片，避免每个关系
    # 再独立锁取并刷新三次。保留素材引用使同内容读取复用当前事务身份。
    locked_materials = db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            col(MaterialFile.id).in_({dist.material_id for dist, _ in rows}),
        )
        .order_by(col(MaterialFile.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    locked_operations = db.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.tenant_id == context.tenant_id,
            col(MaterialAssetOperation.id).in_({op.id for _, op in rows}),
        )
        .order_by(col(MaterialAssetOperation.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    assert locked_materials and locked_operations
    # 取得锁后重新检查完整候选条件；并发消费者已绑定的行不再改写。
    rows = db.exec(
        candidates.where(
            col(MaterialDistribution.id).in_({dist.id for dist, _ in rows})
        ).execution_options(populate_existing=True)
    ).all()
    changed = 0
    ordered = sorted(
        rows, key=lambda pair: (pair[0].advertiser_id, pair[0].material_id)
    )
    for _, grouped in groupby(ordered, key=lambda pair: pair[0].advertiser_id):
        group = list(grouped)
        prepared = 0
        try:
            # 同账户最多20素材的本地登记复用共同权限；保存点退出前重验。
            # 这里只绑定依赖，不调用平台；发送时仍逐请求重新鉴权。
            with db.begin_nested(), local_read_batch(db):
                for dist, operation in group:
                    try:
                        if not resume_seed_dependency(
                            db, context=context, dist=dist, operation=operation
                        ):
                            prepared += 1
                    except DomainError as error:
                        if error.code in {
                            "tiktok_call_deadline_exceeded",
                            "tiktok_local_resources_unavailable",
                        }:
                            raise
                        dist.status, dist.reason_code = "blocked", error.code
                        operation.status = "failed"
                        operation.remote_response = {
                            **operation.remote_response,
                            "definite_no_effect": True,
                            "error_code": error.code,
                        }
        except DomainError as error:
            if error.code in {
                "tiktok_call_deadline_exceeded",
                "tiktok_local_resources_unavailable",
            }:
                raise
            # 最终复核失效时回滚该账户的整片；其它账户继续正常绑定。
            for dist, operation in group:
                dist.status, dist.reason_code = "blocked", error.code
                operation.status = "failed"
                operation.remote_response = {
                    **operation.remote_response,
                    "definite_no_effect": True,
                    "error_code": error.code,
                }
        else:
            changed += prepared
    return changed


def ensure_bc_seed(
    db: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    route: FrozenTikTokRoute,
) -> MaterialBCSeed:
    """内容键串行登记；新准备只接替已明确无效果的终态，历史依赖不改写。"""
    from .distribution import ensure_target_asset

    key = content_key(material)
    lock = int.from_bytes(
        sha256(f"bc-seed:{context.tenant_id}:{route.bc_id}:{key}".encode()).digest()[
            :8
        ],
        "big",
        signed=True,
    )
    db.exec(select(func.pg_advisory_xact_lock(lock))).one()
    seed = db.exec(
        select(MaterialBCSeed)
        .where(
            MaterialBCSeed.tenant_id == context.tenant_id,
            MaterialBCSeed.bc_id == route.bc_id,
            MaterialBCSeed.content_key == key,
        )
        .order_by(col(MaterialBCSeed.generation).desc())
    ).first()
    if seed:
        dist = db.get(MaterialDistribution, seed.distribution_id)
        assert dist
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        assert operation
        # 明确无效果的失败可由用户再次提交准备；不能把超时、已发送或待核实
        # 当成失败重传。即使平台明确拒绝时留下 armed，终态无效果证据仍有效。
        replaceable = (
            dist.status == "blocked"
            and operation.status == "failed"
            and operation.remote_response.get("definite_no_effect") is True
            and operation.attempt_token is None
            and operation.claimed_until is None
            and not operation.remote_response.get("video_id")
            and not operation.remote_response.get("upload_video_id")
        )
        if not replaceable:
            require_same_route(
                load_material_route(
                    dist.target_route, context=context, bc_id=route.bc_id
                ),
                route,
            )
            return seed
    primary = resolve_primary_account(
        db,
        context=context,
        bc_id=route.bc_id,
        route=route,
        advertiser_id=seed.advertiser_id if seed else None,
        persist=True,
    )
    prepared = ensure_target_asset(
        db,
        context=context,
        bc_id=route.bc_id,
        material_id=material.id,
        advertiser_id=primary.advertiser_id,
        task_key=f"bc-seed:{key}",
        route=route,
        _seed_owner=True,
    )
    if prepared.state != "queued" or prepared.task_id is None:
        raise DomainError(
            prepared.reason_code or "material_seed_unavailable",
            prepared.reason_message or "主素材账户转存任务无法准备",
        )
    seed = MaterialBCSeed(
        tenant_id=context.tenant_id,
        bc_id=route.bc_id,
        material_id=material.id,
        advertiser_id=primary.advertiser_id,
        distribution_id=prepared.task_id,
        content_key=key,
        generation=seed.generation + 1 if seed else 1,
    )
    db.add(seed)
    db.flush()
    return seed


def resume_seed_dependency(
    db: Session,
    *,
    context: TenantContext,
    dist: MaterialDistribution,
    operation: MaterialAssetOperation,
) -> bool:
    """返回 True 表示仍等待或已终结；False 才允许正常原生共享发送。"""
    from .distribution import _bind_operation, queue_distribution
    from .readiness import mapping_fresh, target_mapping
    from .remote_sources import resolve_remote_source

    if dist.superseded_by_id is not None or operation.superseded_by_id is not None:
        return True
    if (
        dist.seed_id is None
        or operation.remote_response.get("transport")
        or operation.status != "pending"
        or operation.remote_response.get("send_armed")
        or operation.attempt_token is not None
    ):
        return False
    seed = db.get(MaterialBCSeed, dist.seed_id)
    if seed is None or (seed.tenant_id, seed.bc_id) != (dist.tenant_id, dist.bc_id):
        raise DomainError("material_seed_unavailable", "首次转存依赖身份不匹配")
    target_material = db.get(MaterialFile, dist.material_id)
    source_material = db.get(MaterialFile, seed.material_id)
    if (
        target_material is None
        or source_material is None
        or content_key(target_material) != seed.content_key
        or content_key(source_material) != seed.content_key
    ):
        raise DomainError("material_content_changed", "首次转存内容身份已改变")
    owner = db.get(MaterialDistribution, seed.distribution_id)
    assert owner
    # 未列入显式授权的旧依赖停留原未知；不能自动跟随另一批次的新代。
    if owner.superseded_by_id is not None:
        return True
    route = load_material_route(dist.target_route, context=context, bc_id=dist.bc_id)
    seed_route = load_material_route(
        owner.target_route, context=context, bc_id=dist.bc_id
    )
    require_same_route(seed_route, route)
    for account in {dist.advertiser_id, seed.advertiser_id}:
        require_material_route(
            db,
            context=context,
            route=route,
            bc_id=dist.bc_id,
            advertiser_id=account,
            capability="build",
        )
    if owner.status == "blocked":
        operation.status, dist.status = "failed", "blocked"
        dist.reason_code = owner.reason_code or "material_seed_blocked"
        operation.remote_response = {
            "definite_no_effect": True,
            "error_code": dist.reason_code,
        }
        return True
    if owner.status != "ready":
        dist.status = "result_unknown" if owner.status == "result_unknown" else "queued"
        dist.reason_code = "material_seed_pending"
        # 等待不重复投递。成功回执原子唤醒旧消息；异常落定由正式修复器接续。
        return True
    primary = target_mapping(
        db,
        context=context,
        bc_id=seed.bc_id,
        material_id=seed.material_id,
        advertiser_id=seed.advertiser_id,
    )
    if primary is None or primary.status != "available":
        raise DomainError("material_seed_unavailable", "主素材账户副本尚未核实")
    source = resolve_remote_source(
        db,
        context=context,
        bc_id=seed.bc_id,
        material_id=seed.material_id,
        source_asset_id=primary.id,
    )
    if source is None:
        raise DomainError(
            "material_remote_source_unavailable", "主素材账户副本或读取授权已改变"
        )
    require_material_route(
        db,
        context=context,
        route=seed_route,
        bc_id=seed.bc_id,
        advertiser_id=seed.advertiser_id,
        capability="read",
    )
    if dist.advertiser_id == seed.advertiser_id:
        # 同内容别名指向同一真实账户副本；不能把别名等待者当成另一个目标。
        mapping = target_mapping(
            db,
            context=context,
            bc_id=dist.bc_id,
            material_id=dist.material_id,
            advertiser_id=dist.advertiser_id,
        )
        if mapping is None:
            values = primary.model_dump()
            values.update(id=uuid4(), material_id=dist.material_id)
            mapping = AccountMaterial(**values)
            db.add(mapping)
        else:
            mapping.connection_id = primary.connection_id
            mapping.video_id, mapping.mid = primary.video_id, primary.mid
            mapping.image_id, mapping.cover_url = primary.image_id, primary.cover_url
            mapping.status, mapping.verified_at = primary.status, primary.verified_at
        if not mapping_fresh(mapping):
            # 延迟等待者只能复用真实 VID，过期证据必须沿既有只读路径重新核实。
            operation = _bind_operation(
                db,
                context=context,
                material_id=dist.material_id,
                bc_id=dist.bc_id,
                advertiser_id=dist.advertiser_id,
                path="existing_target",
                route=route,
            )
            dist.operation_id, dist.path = operation.id, "existing_target"
            dist.status, dist.reason_code = "verifying", None
            queue_distribution(db, dist, operation, kind="verify")
            return True
        operation.status, dist.status, dist.reason_code = "succeeded", "ready", None
        operation.remote_response = {
            **operation.remote_response,
            "video_id": primary.video_id,
        }
        return True
    # 等待期间没有远端发送，绑定唯一已核实 primary 来源后才可进入原生共享。
    revision = operation.remote_response.get("revision", 0)
    operation.remote_response = {}
    _bind_operation(
        db,
        context=context,
        material_id=dist.material_id,
        bc_id=dist.bc_id,
        advertiser_id=dist.advertiser_id,
        path="share_source",
        route=route,
        source=primary,
    )
    if operation.remote_response.get("transport") != "native_share":
        raise DomainError("material_seed_unavailable", "目标 BC 来源尚不可原生共享")
    response = operation.remote_response
    operation.remote_response = {**response, "revision": revision}
    dist.source_bc_id = response["source_bc_id"]
    dist.source_material_id = UUID(response["source_material_id"])
    dist.source_asset_id = UUID(response["source_asset_id"])
    dist.source_route = seed_route.model_dump(mode="json")
    dist.status, dist.reason_code = "queued", None
    db.flush()
    return False
