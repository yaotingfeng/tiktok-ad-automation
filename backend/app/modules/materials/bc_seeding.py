"""同内容在目标 BC 仅转存一次；消费者始终保留自己的目标分发身份。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
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


def ensure_bc_seed(
    db: Session,
    *,
    context: TenantContext,
    material: MaterialFile,
    route: FrozenTikTokRoute,
) -> MaterialBCSeed:
    """内容键短事务串行首次登记；FAILED/UNKNOWN 也必须保留原登记。"""
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
        select(MaterialBCSeed).where(
            MaterialBCSeed.tenant_id == context.tenant_id,
            MaterialBCSeed.bc_id == route.bc_id,
            MaterialBCSeed.content_key == key,
        )
    ).first()
    if seed:
        dist = db.get(MaterialDistribution, seed.distribution_id)
        assert dist
        require_same_route(
            load_material_route(dist.target_route, context=context, bc_id=route.bc_id),
            route,
        )
        return seed
    primary = resolve_primary_account(
        db, context=context, bc_id=route.bc_id, route=route, persist=True
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
    from .readiness import target_mapping
    from .remote_sources import resolve_remote_source

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
        # 只增加等待消息代数，不改变 seed 发送代数；旧消息无法重复安排发送。
        operation.remote_response = {
            **operation.remote_response,
            "revision": operation.remote_response.get("revision", 0) + 1,
        }
        queue_distribution(
            db,
            dist,
            operation,
            kind="prepare",
            due=datetime.now(UTC) + timedelta(seconds=30),
        )
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
            mapping = AccountMaterial(
                **(
                    primary.model_dump()
                    | {
                        "id": uuid4(),
                        "material_id": dist.material_id,
                    }
                )
            )
            db.add(mapping)
        else:
            mapping.connection_id = primary.connection_id
            mapping.video_id, mapping.mid = primary.video_id, primary.mid
            mapping.image_id, mapping.cover_url = primary.image_id, primary.cover_url
            mapping.status, mapping.verified_at = primary.status, primary.verified_at
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
