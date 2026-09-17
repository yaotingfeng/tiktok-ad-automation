"""短事务固定主素材账户，不按不同文件的在途数量轮转或串行。

Slots count accepted source operations through final read-back, including UNKNOWN.
They are never reclaimed merely because a worker or HTTP lease expired.
"""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.local_read_batch import reuse_local_read
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from .ingest_models import IngestSessionFile, SourceAccountLoad
from .models import MaterialAssetOperation, MaterialFile
from .routes import require_material_route


def _account_lock(db: Session, *, tenant_id: UUID, advertiser_id: str) -> None:
    key = int.from_bytes(
        sha256(f"source:{tenant_id}:{advertiser_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    # 仅等待短账本事务，不持锁执行 HTTP；锁竞争不能把文件分散到其他源账户。
    db.exec(select(func.pg_advisory_xact_lock(key))).one()


def source_file(
    db: Session, *, context: TenantContext, bc_id: str, material_id: UUID
) -> IngestSessionFile:
    material = db.exec(
        select(MaterialFile)
        .where(
            MaterialFile.tenant_id == context.tenant_id,
            MaterialFile.bc_id == bc_id,
            MaterialFile.id == material_id,
        )
        .with_for_update()
    ).one_or_none()
    if material is None:
        raise DomainError("material_not_found", "未找到当前租户素材")
    # A generation belongs to one ingest identity. Ambiguous links cannot choose
    # a source or charge another import's current occupancy.
    rows = db.exec(
        select(IngestSessionFile)
        .where(
            IngestSessionFile.tenant_id == context.tenant_id,
            IngestSessionFile.bc_id == bc_id,
            IngestSessionFile.material_id == material_id,
        )
        .limit(2)
        .with_for_update()
    ).all()
    if len(rows) != 1:
        raise DomainError("ingest_identity_unverified", "素材导入身份缺失或存在歧义")
    assert isinstance(rows[0], IngestSessionFile)
    return rows[0]


def _exact_access(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    connection_id: UUID,
) -> AccountAccess:
    access = resolve_account_access(
        db,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        action="upload",
        connection_id=connection_id,
    )
    grant = db.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="upload").where(
            BCAccountAccess.advertiser_id == advertiser_id,
            BCAccountAccess.connection_id == connection_id,
        )
    ).first()
    if grant is None:
        raise DomainError("upload_connection_changed", "实际上传连接已失去授权")
    return access.model_copy(update={"connection_id": connection_id})


def resolve_primary_account(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    route: FrozenTikTokRoute,
    advertiser_id: str | None = None,
    persist: bool = False,
) -> AccountAccess:
    """复用主账户、授权和冷却检查；预览只检查，提交才固定选择。"""
    now = datetime.now(UTC)
    # 冷却按实际账户聚合；换授权连接不绕开已观察到的上游限制。
    loads = (
        select(
            col(SourceAccountLoad.advertiser_id),
            func.max(SourceAccountLoad.cooldown_until).label("cooldown"),
        )
        .where(SourceAccountLoad.tenant_id == context.tenant_id)
        .group_by(SourceAccountLoad.advertiser_id)
        .subquery()
    )
    grant_query = usable_grants(
        tenant_id=context.tenant_id, bc_id=bc_id, action="upload"
    ).where(BCAccountAccess.connection_id == route.connection_id)
    if advertiser_id is None:
        # NO KEY UPDATE 保护首次选择且不阻塞其他素材的 BC 外键检查。
        bc_query = (
            select(TenantBC)
            .where(TenantBC.tenant_id == context.tenant_id, TenantBC.bc_id == bc_id)
            .execution_options(populate_existing=True)
        )
        if persist:
            bc_query = bc_query.with_for_update(key_share=True)
        bc = db.exec(bc_query).one()
        advertiser_id = bc.material_advertiser_id
        if advertiser_id is None:
            grants = grant_query.subquery()
            advertiser_id = db.exec(
                select(grants.c.advertiser_id)
                .outerjoin(loads, grants.c.advertiser_id == loads.c.advertiser_id)
                .where((loads.c.cooldown.is_(None)) | (loads.c.cooldown <= now))
                .order_by(grants.c.advertiser_id)
                .limit(1)
            ).first()
            if advertiser_id is None:
                any_grant = db.exec(grant_query.limit(1)).first()
                raise DomainError(
                    "source_capacity_pending" if any_grant else "no_upload_account",
                    "合法来源处于冷却期"
                    if any_grant
                    else "当前 BC 没有可上传的授权账户",
                    retryable=any_grant is not None,
                )
            if persist:
                bc.material_advertiser_id = advertiser_id
    require_material_route(
        db,
        context=context,
        route=route,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        capability="upload",
    )
    access = _exact_access(
        db,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        connection_id=route.connection_id,
    )
    if persist:
        _account_lock(db, tenant_id=context.tenant_id, advertiser_id=advertiser_id)
    cooldown = db.exec(
        select(func.max(SourceAccountLoad.cooldown_until)).where(
            SourceAccountLoad.tenant_id == context.tenant_id,
            SourceAccountLoad.advertiser_id == advertiser_id,
        )
    ).one()
    if cooldown and cooldown > now:
        raise DomainError(
            "source_capacity_pending", "主素材账户处于冷却期", retryable=True
        )
    return access


@reuse_local_read
def read_primary_advertiser(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    route: FrozenTikTokRoute,
) -> str:
    # 与持久选择入口分开：预览可复用只读选择，提交/上传永远实时加锁并核验。
    return resolve_primary_account(
        db, context=context, bc_id=bc_id, route=route
    ).advertiser_id


def claim_source_account(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    reselect: bool = False,
    route: FrozenTikTokRoute,
    advertiser_id: str | None = None,
) -> AccountAccess:
    """Persist the exact assignment and slot once; caller creates its operation.

    Caller commits this transaction with the operation's source_slot_held marker.
    Reselection is reserved for proven-unsent failed operations, never UNKNOWN.
    """
    require_tenant(
        db, tenant_id=context.tenant_id, actor_id=context.actor_id, action="upload"
    )
    row = source_file(db, context=context, bc_id=bc_id, material_id=material_id)
    if advertiser_id is not None:
        # 指定账户的恢复仍走同一授权、租户和 BC 校验，不能落到其他候选账户。
        require_material_route(
            db,
            context=context,
            route=route,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            capability="upload",
        )
        if (
            row.source_advertiser_id
            and not reselect
            and row.source_advertiser_id != advertiser_id
        ):
            raise DomainError("frozen_route_changed", "已有来源账户不能直接替换")
    if row.source_advertiser_id and not reselect:
        assert row.connection_id
        if row.connection_id != route.connection_id:
            raise DomainError("frozen_route_changed", "来源账户与原上传连接不一致")
        require_material_route(
            db,
            context=context,
            route=route,
            bc_id=bc_id,
            advertiser_id=row.source_advertiser_id,
            capability="upload",
        )
        return _exact_access(
            db,
            context=context,
            bc_id=bc_id,
            advertiser_id=row.source_advertiser_id,
            connection_id=row.connection_id,
        )
    if reselect:
        unsafe_prior = db.exec(
            select(MaterialAssetOperation.id)
            .where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.bc_id == bc_id,
                MaterialAssetOperation.material_id == material_id,
                MaterialAssetOperation.path == "upload_original",
                (col(MaterialAssetOperation.status) != "failed")
                | col(MaterialAssetOperation.remote_response)["send_armed"]
                .as_boolean()
                .is_(True)
                | col(MaterialAssetOperation.remote_response)["source_slot_held"]
                .as_boolean()
                .is_(True),
            )
            .limit(1)
            .with_for_update()
        ).first()
        if unsafe_prior is not None:
            raise DomainError(
                "material_retry_not_allowed", "已有源操作尚未证实无远端效果"
            )
    access = resolve_primary_account(
        db,
        context=context,
        bc_id=bc_id,
        route=route,
        advertiser_id=advertiser_id,
        persist=True,
    )
    advertiser_id = access.advertiser_id
    now = datetime.now(UTC)
    load = db.get(
        SourceAccountLoad,
        (context.tenant_id, bc_id, advertiser_id, route.connection_id),
    )
    if load is None:
        load = SourceAccountLoad(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            connection_id=route.connection_id,
        )
        db.add(load)
    # 只记真实未结束操作的数量，不再把统计值用作单账户串行门槛。
    load.in_flight += 1
    load.revision += 1
    load.last_assigned_at = now
    row.source_advertiser_id, row.connection_id = advertiser_id, route.connection_id
    db.flush()
    return access


def release_source_account(
    db: Session,
    *,
    context: TenantContext,
    operation: MaterialAssetOperation,
    cooldown_until: datetime | None = None,
) -> bool:
    """Release only under caller-held material/object/operation locks and proof.

    Marker and counter are updated atomically; repeated successful callbacks are
    no-ops. UNKNOWN and armed failed operations cannot release a live slot.
    """
    locked = db.exec(
        select(MaterialAssetOperation)
        .where(
            MaterialAssetOperation.id == operation.id,
            MaterialAssetOperation.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if not locked.remote_response.get("source_slot_held"):
        return False
    if locked.status != "succeeded" and not (
        locked.status == "failed" and not locked.remote_response.get("send_armed")
    ):
        raise DomainError(
            "material_result_pending", "源操作结果未核实，不能释放来源占用"
        )
    _account_lock(db, tenant_id=context.tenant_id, advertiser_id=locked.advertiser_id)
    load = db.get(
        SourceAccountLoad,
        (
            context.tenant_id,
            locked.bc_id,
            locked.advertiser_id,
            UUID(locked.remote_response["connection_id"]),
        ),
        with_for_update=True,
    )
    if load is None or load.in_flight < 1:
        raise DomainError("source_load_inconsistent", "来源占用需要核对")
    load.in_flight -= 1
    load.revision += 1
    if cooldown_until is not None:
        load.cooldown_until = cooldown_until
    locked.remote_response = {**locked.remote_response, "source_slot_held": False}
    db.flush()
    return True
