from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.errors import ERROR_HTTP_STATUS, DomainError
from app.core.pagination import Page
from app.integrations.tiktok.auth import (
    _configured_authorization_url,
    start_authorization,
)
from app.modules.accounts.access import OPERABLE_REMOTE_STATUSES
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.resolver import (
    bc_directory_scope,
    decode_cursor,
    encode_cursor,
    resolve_lines,
)
from app.modules.accounts.schemas import (
    AccountPublic,
    AppConfiguration,
    AuthorizationRequest,
    AuthorizationURL,
    Availability,
    BCPublic,
    ConnectionPublic,
    ConnectionUpdate,
    ResolvedLine,
    ResolveRequest,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["accounts"])
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=4096)]
QueryText = Annotated[str, Query(max_length=255)]
BCID = Annotated[str, Query(min_length=1, max_length=128)]


@router.get("/accounts", response_model=Page[AccountPublic])
def get_accounts(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    bc_id: BCID,
    query: QueryText = "",
    cursor: Cursor = None,
    limit: Limit = 50,
    remote_status: str | None = None,
    availability: Availability | None = None,
) -> Page[AccountPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    bc = session.get(TenantBC, (tenant_id, bc_id), populate_existing=True)
    if bc is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    scope = {
        "kind": "accounts",
        "tenant_id": str(tenant_id),
        "bc_id": bc_id,
        "query": query,
        "remote_status": remote_status,
        "availability": availability,
    }
    last_id = decode_cursor(cursor, scope=scope)
    live = (
        select(BCAccountAccess.advertiser_id)
        .join(
            TikTokConnection,
            and_(
                col(TikTokConnection.id) == BCAccountAccess.connection_id,
                col(TikTokConnection.tenant_id) == BCAccountAccess.tenant_id,
            ),
        )
        .where(
            BCAccountAccess.tenant_id == tenant_id,
            BCAccountAccess.bc_id == bc_id,
            BCAccountAccess.advertiser_id == AdvertiserAccount.advertiser_id,
            col(BCAccountAccess.in_bc).is_(True),
            col(BCAccountAccess.authorized).is_(True),
            col(BCAccountAccess.active).is_(True),
            TikTokConnection.status == "ACTIVE",
        )
    )
    conflict = or_(
        col(AdvertiserAccount.ownership_conflict).is_(True),
        literal(bc.ownership_conflict),
    )
    missing = or_(
        func.trim(col(AdvertiserAccount.currency)) == "",
        func.trim(col(AdvertiserAccount.timezone)) == "",
    )
    operable = and_(
        ~conflict,
        ~missing,
        col(AdvertiserAccount.remote_status).in_(OPERABLE_REMOTE_STATUSES),
    )
    build = and_(
        operable,
        live.where(
            BCAccountAccess.permission_state == "VERIFIED",
            col(BCAccountAccess.can_build).is_(True),
        ).exists(),
    )
    upload = and_(
        operable,
        live.where(
            BCAccountAccess.permission_state == "VERIFIED",
            col(BCAccountAccess.can_upload).is_(True),
        ).exists(),
    )
    verified = live.where(BCAccountAccess.permission_state == "VERIFIED").exists()
    permission = case(
        (missing, "METADATA_INCOMPLETE"), (verified, "VERIFIED"), else_="UNKNOWN"
    )
    unknown = live.where(BCAccountAccess.permission_state != "VERIFIED").exists()
    available = case(
        (conflict, "OWNERSHIP_CONFLICT"),
        (missing, "METADATA_INCOMPLETE"),
        (or_(build, upload), "AVAILABLE"),
        (unknown, "PERMISSION_UNKNOWN"),
        else_="NO_ACCESS",
    )
    checked = (
        select(func.max(col(BCAccountAccess.checked_at)))
        .where(
            BCAccountAccess.tenant_id == tenant_id,
            BCAccountAccess.bc_id == bc_id,
            BCAccountAccess.advertiser_id == AdvertiserAccount.advertiser_id,
        )
        .scalar_subquery()
    )
    statement = sa_select(
        AdvertiserAccount,
        build.label("can_build"),
        upload.label("can_upload"),
        available.label("availability"),
        checked.label("checked_at"),
        permission.label("permission_state"),
    ).where(
        col(AdvertiserAccount.tenant_id) == tenant_id,
        bc_directory_scope(tenant_id, bc_id),
    )
    if query.strip():
        statement = statement.where(
            or_(
                col(AdvertiserAccount.name).icontains(query.strip(), autoescape=True),
                col(AdvertiserAccount.advertiser_id) == query.strip(),
            )
        )
    if last_id is not None:
        statement = statement.where(col(AdvertiserAccount.advertiser_id) > last_id)
    if remote_status is not None:
        statement = statement.where(
            col(AdvertiserAccount.remote_status) == remote_status
        )
    if availability is not None:
        statement = statement.where(available == availability)
    rows = SQLAlchemySession.execute(
        session,
        statement.order_by(col(AdvertiserAccount.advertiser_id))
        .limit(limit + 1)
        .execution_options(populate_existing=True),
    ).all()
    items = []
    for account, can_build, can_upload, state, checked_at, permission_state in rows[
        :limit
    ]:
        items.append(
            AccountPublic(
                advertiser_id=account.advertiser_id,
                bc_id=bc_id,
                name=account.name,
                currency=account.currency,
                timezone=account.timezone,
                remote_status=account.remote_status,
                ownership_conflict=account.ownership_conflict or bc.ownership_conflict,
                can_build=can_build,
                can_upload=can_upload,
                permission_state=permission_state,
                availability=cast(Availability, state),
                checked_at=checked_at,
            )
        )
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=items[-1].advertiser_id)
        if len(rows) > limit
        else None,
    )


@router.post("/accounts/resolve", response_model=list[ResolvedLine])
def post_resolve(
    tenant_id: UUID, body: ResolveRequest, session: SessionDep, user: CurrentUser
) -> list[ResolvedLine]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return resolve_lines(session, context=context, bc_id=body.bc_id, lines=body.lines)


@router.get("/bcs", response_model=Page[BCPublic])
def get_bcs(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    query: QueryText = "",
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[BCPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    scope = {"kind": "bcs", "tenant_id": str(tenant_id), "query": query}
    last_id = decode_cursor(cursor, scope=scope)
    statement = select(TenantBC).where(TenantBC.tenant_id == tenant_id)
    if last_id is not None:
        statement = statement.where(TenantBC.bc_id > last_id)
    if query.strip():
        statement = statement.where(
            or_(
                col(TenantBC.name).icontains(query.strip(), autoescape=True),
                col(TenantBC.bc_id) == query.strip(),
            )
        )
    rows = session.exec(
        statement.order_by(col(TenantBC.bc_id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    items = [BCPublic.model_validate(row) for row in rows[:limit]]
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=items[-1].bc_id)
        if len(rows) > limit
        else None,
    )


@router.get("/tiktok/connections", response_model=Page[ConnectionPublic])
def get_connections(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    status: str | None = None,
) -> Page[ConnectionPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    scope = {"kind": "connections", "tenant_id": str(tenant_id), "status": status}
    last_id = decode_cursor(cursor, scope=scope)
    latest = (
        select(DiscoveryRun)
        .where(
            DiscoveryRun.tenant_id == tenant_id,
            DiscoveryRun.connection_id == TikTokConnection.id,
        )
        .order_by(col(DiscoveryRun.created_at).desc(), col(DiscoveryRun.id).desc())
        .limit(1)
    )
    latest_time = latest.with_only_columns(
        col(DiscoveryRun.created_at)
    ).scalar_subquery()
    latest_error = latest.with_only_columns(
        col(DiscoveryRun.error_code)
    ).scalar_subquery()
    statement = select(TikTokConnection, latest_time, latest_error).where(
        TikTokConnection.tenant_id == tenant_id
    )
    if last_id is not None:
        try:
            last_uuid = UUID(last_id)
        except ValueError:
            raise DomainError("invalid_cursor", "连接游标无效") from None
        statement = statement.where(TikTokConnection.id > last_uuid)
    if status is not None:
        statement = statement.where(TikTokConnection.status == status)
    rows = session.exec(
        statement.order_by(col(TikTokConnection.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    ).all()
    items = []
    for connection, last_discovery, error_code in rows[:limit]:
        item = ConnectionPublic.model_validate(connection)
        item.last_discovery = last_discovery
        item.error_code = (
            error_code
            if error_code in ERROR_HTTP_STATUS
            else "discovery_failed"
            if error_code
            else None
        )
        items.append(item)
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].id))
        if len(rows) > limit
        else None,
    )


@router.get("/tiktok/configuration", response_model=AppConfiguration)
def get_configuration(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> AppConfiguration:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    missing = settings.tiktok_app_missing_fields.copy()
    if not settings.TIKTOK_AUTHORIZATION_URL:
        missing.append("TIKTOK_AUTHORIZATION_URL")
    try:
        _configured_authorization_url()
    except DomainError as error:
        if error.code == "connection_encryption_unconfigured":
            missing.append("CONNECTION_ENCRYPTION_KEY")
        return AppConfiguration(
            configured=False,
            status="NOT_CONFIGURED"
            if len(settings.tiktok_app_missing_fields) == 3
            else "INCOMPLETE",
            missing_fields=missing,
            code=error.code,
        )
    return AppConfiguration(
        configured=True, status="READY", missing_fields=[], code=None
    )


@router.post("/tiktok/authorizations", response_model=AuthorizationURL)
def post_authorization(
    tenant_id: UUID,
    body: AuthorizationRequest,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> AuthorizationURL:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    if body.connection_id is not None:
        connection = session.exec(
            select(TikTokConnection)
            .where(
                TikTokConnection.id == body.connection_id,
                TikTokConnection.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if connection is None:
            raise DomainError("connection_not_found", "当前租户连接不存在")
        if connection.status == "DISABLED":
            raise DomainError(
                "connection_unavailable", "已停用连接不能重新授权，请创建新连接"
            )
    url = start_authorization(
        session, context=context, connection_id=body.connection_id
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return AuthorizationURL(url=url)


@router.patch("/tiktok/connections/{connection_id}", response_model=ConnectionPublic)
def patch_connection(
    tenant_id: UUID,
    connection_id: UUID,
    body: ConnectionUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> ConnectionPublic:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.tenant_id == tenant_id,
            TikTokConnection.id == connection_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None:
        raise DomainError("connection_not_found", "当前租户连接不存在")
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    if connection.status != body.status:
        connection.status = body.status
        session.add(connection)
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=user.id,
                action="tiktok.connection.disable",
                target_id=str(connection_id),
            )
        )
    result = ConnectionPublic.model_validate(connection)
    session.commit()
    return result
