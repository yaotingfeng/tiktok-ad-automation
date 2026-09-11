from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy import cast as sql_cast
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import JSONB
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
from app.modules.accounts.connections import disable_connection
from app.modules.accounts.models import (
    AdvertiserAccount,
    AuthorizationAttempt,
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
    DiscoveryStatus,
    ResolvedLine,
    ResolveRequest,
)
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
    connection_id: UUID | None = None,
) -> Page[BCPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    scope = {
        "kind": "bcs",
        "tenant_id": str(tenant_id),
        "query": query,
        "connection_id": str(connection_id) if connection_id else None,
    }
    last_id = decode_cursor(cursor, scope=scope)
    statement = select(TenantBC).where(TenantBC.tenant_id == tenant_id)
    if connection_id is not None:
        connection = session.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == tenant_id,
                TikTokConnection.id == connection_id,
            )
        ).one_or_none()
        if connection is None:
            raise DomainError("connection_not_found", "当前租户连接不存在")
        # Keep this in SQL: a large connection snapshot never becomes an API list
        # or Python IN clause. Empty BCs are present even without any account grants.
        latest_work = (
            select(col(DiscoveryRun.work))
            .where(
                DiscoveryRun.tenant_id == tenant_id,
                DiscoveryRun.connection_id == connection_id,
                DiscoveryRun.status == "COMPLETE",
            )
            .order_by(
                col(DiscoveryRun.completed_at).desc().nulls_last(),
                col(DiscoveryRun.id).desc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        statement = statement.where(
            sql_cast(latest_work, JSONB)["bc_ids"].contains(
                func.jsonb_build_array(col(TenantBC.bc_id))
            )
        )
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
    latest_status = latest.with_only_columns(col(DiscoveryRun.status)).scalar_subquery()
    # Reauthorization keeps old credentials ACTIVE. Expose the candidate's
    # separate progress, including the interval before its worker creates a run.
    pending_attempt = (
        select(col(AuthorizationAttempt.id))
        .where(
            AuthorizationAttempt.tenant_id == tenant_id,
            AuthorizationAttempt.connection_id == TikTokConnection.id,
            AuthorizationAttempt.base_credential_revision
            == TikTokConnection.credential_revision,
            AuthorizationAttempt.status == "CANDIDATE_READY",
        )
        .order_by(
            col(AuthorizationAttempt.claimed_at).desc().nulls_last(),
            col(AuthorizationAttempt.id).desc(),
        )
        .limit(1)
        .correlate(TikTokConnection)
        .scalar_subquery()
    )
    pending_status = (
        select(col(DiscoveryRun.status))
        .where(
            DiscoveryRun.tenant_id == tenant_id,
            DiscoveryRun.connection_id == TikTokConnection.id,
            DiscoveryRun.candidate_attempt_id == pending_attempt,
        )
        .order_by(
            col(DiscoveryRun.created_at).desc(),
            col(DiscoveryRun.id).desc(),
        )
        .limit(1)
        .correlate(TikTokConnection)
        .scalar_subquery()
    )
    progress = case(
        (pending_attempt.is_not(None), func.coalesce(pending_status, "QUEUED")),
        else_=latest_status,
    )
    authorized_time = (
        select(func.max(col(DiscoveryRun.completed_at)))
        .join(
            AuthorizationAttempt,
            and_(
                col(AuthorizationAttempt.tenant_id) == DiscoveryRun.tenant_id,
                col(AuthorizationAttempt.id) == DiscoveryRun.candidate_attempt_id,
                col(AuthorizationAttempt.connection_id) == DiscoveryRun.connection_id,
            ),
        )
        .where(
            DiscoveryRun.tenant_id == tenant_id,
            DiscoveryRun.connection_id == TikTokConnection.id,
            DiscoveryRun.status == "COMPLETE",
            AuthorizationAttempt.status == "ACCEPTED",
        )
        .scalar_subquery()
    )
    statement = sa_select(
        TikTokConnection, latest_time, latest_error, authorized_time, progress
    ).where(col(TikTokConnection.tenant_id) == tenant_id)
    if last_id is not None:
        try:
            last_uuid = UUID(last_id)
        except ValueError:
            raise DomainError("invalid_cursor", "连接游标无效") from None
        statement = statement.where(col(TikTokConnection.id) > last_uuid)
    if status is not None:
        statement = statement.where(col(TikTokConnection.status) == status)
    rows = SQLAlchemySession.execute(
        session,
        statement.order_by(col(TikTokConnection.id))
        .limit(limit + 1)
        .execution_options(populate_existing=True),
    ).all()
    items = []
    for (
        connection,
        last_discovery,
        error_code,
        last_authorized_at,
        discovery_status,
    ) in rows[:limit]:
        item = ConnectionPublic.model_validate(connection)
        item.last_discovery = last_discovery
        item.last_authorized_at = last_authorized_at
        item.discovery_status = cast(DiscoveryStatus | None, discovery_status)
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
    body: ConnectionUpdate,  # noqa: ARG001 — 保留 HTTP 请求的仅停用状态校验。
    session: SessionDep,
    user: CurrentUser,
) -> ConnectionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    # 两个停用入口共用授权版本与候选清理，避免旧页面留下可继续发布的候选。
    disable_connection(
        session,
        context=context,
        connection_id=connection_id,
        task_deadline=datetime.now(UTC) + timedelta(seconds=5),
    )
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.tenant_id == tenant_id,
            TikTokConnection.id == connection_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    result = ConnectionPublic.model_validate(connection)
    session.commit()
    return result
