"""Short PostgreSQL transactions choose actual source accounts fairly.

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
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.schemas import AccountAccess
from app.modules.tenants.permissions import require_tenant

from .ingest_models import IngestSessionFile, SourceAccountLoad
from .models import MaterialAssetOperation, MaterialFile

SOURCE_MAX_INFLIGHT = 1
CANDIDATE_WINDOW = 100


def _account_lock(db: Session, *, tenant_id: UUID, advertiser_id: str) -> bool:
    key = int.from_bytes(
        sha256(f"source:{tenant_id}:{advertiser_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    return bool(db.exec(select(func.pg_try_advisory_xact_lock(key))).one())


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
        db, context=context, bc_id=bc_id, advertiser_id=advertiser_id, action="upload"
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


def claim_source_account(
    db: Session,
    *,
    context: TenantContext,
    bc_id: str,
    material_id: UUID,
    reselect: bool = False,
) -> AccountAccess:
    """Persist the exact assignment and slot once; caller creates its operation.

    Caller commits this transaction with the operation's source_slot_held marker.
    Reselection is reserved for proven-unsent failed operations, never UNKNOWN.
    """
    require_tenant(
        db, tenant_id=context.tenant_id, actor_id=context.actor_id, action="upload"
    )
    row = source_file(db, context=context, bc_id=bc_id, material_id=material_id)
    if row.source_advertiser_id and not reselect:
        assert row.connection_id
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
    now = datetime.now(UTC)
    # Aggregate by advertiser across connection revisions. Refreshing OAuth must
    # not create a second slot for an account whose previous result is unknown.
    loads = (
        select(
            col(SourceAccountLoad.advertiser_id),
            func.sum(SourceAccountLoad.in_flight).label("in_flight"),
            func.max(SourceAccountLoad.cooldown_until).label("cooldown"),
            func.max(SourceAccountLoad.last_assigned_at).label("last_assigned"),
        )
        .where(SourceAccountLoad.tenant_id == context.tenant_id)
        .group_by(SourceAccountLoad.advertiser_id)
        .subquery()
    )
    grants = usable_grants(
        tenant_id=context.tenant_id, bc_id=bc_id, action="upload"
    ).subquery()
    candidates = db.exec(
        select(grants.c.advertiser_id, grants.c.connection_id)
        .outerjoin(loads, grants.c.advertiser_id == loads.c.advertiser_id)
        .where(
            func.coalesce(loads.c.in_flight, 0) < SOURCE_MAX_INFLIGHT,
            (loads.c.cooldown.is_(None)) | (loads.c.cooldown <= now),
        )
        .order_by(
            func.coalesce(loads.c.in_flight, 0),
            loads.c.last_assigned.asc().nulls_first(),
            loads.c.cooldown.asc().nulls_first(),
            grants.c.advertiser_id,
            grants.c.connection_id,
        )
        .limit(CANDIDATE_WINDOW)
    ).all()
    for advertiser_id, connection_id in candidates:
        if not _account_lock(
            db, tenant_id=context.tenant_id, advertiser_id=advertiser_id
        ):
            continue
        occupied = db.exec(
            select(
                func.coalesce(func.sum(SourceAccountLoad.in_flight), 0),
                func.max(SourceAccountLoad.cooldown_until),
            ).where(
                SourceAccountLoad.tenant_id == context.tenant_id,
                SourceAccountLoad.advertiser_id == advertiser_id,
            )
        ).one()
        if occupied[0] >= SOURCE_MAX_INFLIGHT or (occupied[1] and occupied[1] > now):
            continue
        access = _exact_access(
            db,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            connection_id=connection_id,
        )
        load = db.get(
            SourceAccountLoad, (context.tenant_id, bc_id, advertiser_id, connection_id)
        )
        if load is None:
            load = SourceAccountLoad(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                connection_id=connection_id,
            )
            db.add(load)
        load.in_flight += 1
        load.revision += 1
        load.last_assigned_at = now
        row.source_advertiser_id, row.connection_id = advertiser_id, connection_id
        db.flush()
        return access
    any_grant = db.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="upload").limit(
            1
        )
    ).first()
    raise DomainError(
        "source_capacity_pending" if any_grant else "no_upload_account",
        "当前合法来源暂满或处于冷却期" if any_grant else "当前 BC 没有可上传的授权账户",
        retryable=any_grant is not None,
    )


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
    if not _account_lock(
        db, tenant_id=context.tenant_id, advertiser_id=locked.advertiser_id
    ):
        raise DomainError("source_capacity_pending", "来源占用正在更新", retryable=True)
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
