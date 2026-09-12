from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.errors import ERROR_HTTP_STATUS, DomainError
from app.core.pagination import Page
from app.integrations.tiktok.auth import (
    start_authorization,
)
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import OPERABLE_REMOTE_STATUSES
from app.modules.accounts.channel_configuration import channel_configuration
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.connection_views import enrich_connections
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
    decode_cursor,
    encode_cursor,
    resolve_lines,
)
from app.modules.accounts.routing import set_default_route
from app.modules.accounts.schemas import (
    AccountPublic,
    AppConfiguration,
    AuthorizationRequest,
    AuthorizationURL,
    Availability,
    BCPublic,
    ConnectionPublic,
    ConnectionUpdate,
    DefaultConnectionRequest,
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
    connection_id: UUID | None = None,
) -> Page[AccountPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    bc = session.get(TenantBC, (tenant_id, bc_id), populate_existing=True)
    if bc is None:
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")
    # 目录浏览也固定一条连接，但允许查看已停用连接的本地历史与权限状态。
    if connection_id is None:
        default = session.get(
            BCDefaultRoute, (tenant_id, bc_id), populate_existing=True
        )
        if default is None:
            raise DomainError(
                "bc_default_connection_required", "请先选择当前 BC 的连接"
            )
        connection_id = default.connection_id
    if session.get(BCConnectionBinding, (tenant_id, bc_id, connection_id)) is None:
        raise DomainError("connection_bc_mismatch", "连接未绑定当前 BC")
    scope = {
        "kind": "accounts",
        "tenant_id": str(tenant_id),
        "bc_id": bc_id,
        "connection_id": str(connection_id),
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
            BCAccountAccess.connection_id == connection_id,
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
    now = datetime.now(UTC)
    minimum = now - timedelta(seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS)
    authorization = (
        select(ConnectionAuthorization.id)
        .join(
            TikTokConnection,
            (col(TikTokConnection.id) == col(ConnectionAuthorization.connection_id))
            & (
                col(TikTokConnection.tenant_id)
                == col(ConnectionAuthorization.tenant_id)
            )
            & (
                col(TikTokConnection.authorization_revision)
                == col(ConnectionAuthorization.authorization_revision)
            ),
        )
        .where(
            ConnectionAuthorization.tenant_id == tenant_id,
            ConnectionAuthorization.connection_id == connection_id,
            func.jsonb_array_length(col(ConnectionAuthorization.scopes)) > 0,
            ConnectionAuthorization.source != "UNKNOWN",
            ConnectionAuthorization.source != "",
            col(ConnectionAuthorization.verified_at).between(minimum, now),
        )
    )
    live = live.where(col(BCAccountAccess.checked_at).between(minimum, now))
    # 共享授权可用不代表此 BC 同步/绑定仍有效。
    live = live.where(
        select(BCConnectionBinding.connection_id)
        .where(
            BCConnectionBinding.tenant_id == tenant_id,
            BCConnectionBinding.bc_id == bc_id,
            BCConnectionBinding.connection_id == connection_id,
            BCConnectionBinding.status == "ACTIVE",
        )
        .exists()
    )
    build = and_(
        operable,
        authorization.where(
            ConnectionAuthorization.permission_summary["build_authorized"]
            == literal(True, type_=JSONB)
        ).exists(),
        live.where(
            BCAccountAccess.permission_state == "VERIFIED",
            col(BCAccountAccess.can_build).is_(True),
        ).exists(),
    )
    upload = and_(
        operable,
        authorization.where(
            ConnectionAuthorization.permission_summary["upload_authorized"]
            == literal(True, type_=JSONB)
        ).exists(),
        live.where(
            BCAccountAccess.permission_state == "VERIFIED",
            col(BCAccountAccess.can_upload).is_(True),
        ).exists(),
    )
    verified = and_(
        authorization.exists(),
        live.where(BCAccountAccess.permission_state == "VERIFIED").exists(),
    )
    permission = case(
        (missing, "METADATA_INCOMPLETE"), (verified, "VERIFIED"), else_="UNKNOWN"
    )
    unknown = ~verified
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
            BCAccountAccess.connection_id == connection_id,
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
        select(BCAccountAccess.advertiser_id)
        .where(
            BCAccountAccess.tenant_id == tenant_id,
            BCAccountAccess.bc_id == bc_id,
            BCAccountAccess.connection_id == connection_id,
            BCAccountAccess.advertiser_id == AdvertiserAccount.advertiser_id,
        )
        .exists(),
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
    if connection_id is None:
        # 保留从未接入的目录记录；已有接入全部解绑后，不再作为可切换工作区。
        bound = select(BCConnectionBinding.connection_id).where(
            BCConnectionBinding.tenant_id == tenant_id,
            BCConnectionBinding.bc_id == TenantBC.bc_id,
        )
        statement = statement.where(
            or_(
                ~bound.exists(),
                bound.where(BCConnectionBinding.status != "DISABLED").exists(),
            )
        )
    if connection_id is not None:
        connection = session.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == tenant_id,
                TikTokConnection.id == connection_id,
            )
        ).one_or_none()
        if connection is None:
            raise DomainError("connection_not_found", "当前租户连接不存在")
        statement = statement.where(
            select(BCConnectionBinding.connection_id)
            .where(
                BCConnectionBinding.tenant_id == tenant_id,
                BCConnectionBinding.connection_id == connection_id,
                BCConnectionBinding.bc_id == TenantBC.bc_id,
                BCConnectionBinding.status != "DISABLED",
            )
            .exists()
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
    if items and connection_id is not None:
        item_ids = [item.bc_id for item in items]
        bindings = {
            row.bc_id: row
            for row in session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.tenant_id == tenant_id,
                    BCConnectionBinding.connection_id == connection_id,
                    col(BCConnectionBinding.bc_id).in_(item_ids),
                )
            ).all()
        }
        runs = {
            row.bc_id: row
            for row in session.exec(
                select(DiscoveryRun)
                .where(
                    DiscoveryRun.tenant_id == tenant_id,
                    DiscoveryRun.connection_id == connection_id,
                    col(DiscoveryRun.bc_id).in_(item_ids),
                )
                .distinct(col(DiscoveryRun.bc_id))
                .order_by(
                    col(DiscoveryRun.bc_id),
                    col(DiscoveryRun.created_at).desc(),
                    col(DiscoveryRun.id).desc(),
                )
            ).all()
        }
        for item in items:
            binding = bindings.get(item.bc_id)
            run = runs.get(item.bc_id)
            if binding is not None:
                item.binding_status = cast(
                    Literal["SYNCING", "ACTIVE", "ERROR", "DISABLED"], binding.status
                )
                item.error_code = (
                    binding.last_error_code
                    if binding.last_error_code in ERROR_HTTP_STATUS
                    else None
                )
            if run is not None:
                item.last_discovery = run.completed_at or run.created_at
                item.discovery_status = cast(DiscoveryStatus, run.status)
                if run.error_code in ERROR_HTTP_STATUS:
                    item.error_code = run.error_code
    if items:
        default_connections = dict(
            session.exec(
                select(BCDefaultRoute.bc_id, BCDefaultRoute.connection_id).where(
                    BCDefaultRoute.tenant_id == tenant_id,
                    col(BCDefaultRoute.bc_id).in_([item.bc_id for item in items]),
                )
            ).all()
        )
        for item in items:
            item.default_connection_id = default_connections.get(item.bc_id)
            item.is_default = (
                connection_id is not None
                and item.default_connection_id == connection_id
            )
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
    bc_id: BCID | None = None,
) -> Page[ConnectionPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    scope = {
        "kind": "connections",
        "tenant_id": str(tenant_id),
        "status": status,
        "bc_id": bc_id,
    }
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
    if bc_id is not None:
        if session.get(TenantBC, (tenant_id, bc_id)) is None:
            raise DomainError("account_not_in_bc", "当前租户没有该 BC")
        statement = statement.where(
            select(BCConnectionBinding.connection_id)
            .where(
                BCConnectionBinding.tenant_id == tenant_id,
                BCConnectionBinding.bc_id == bc_id,
                BCConnectionBinding.connection_id == TikTokConnection.id,
                BCConnectionBinding.status != "DISABLED",
            )
            .exists()
        )
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
    enrich_connections(session, context=context, items=items, bc_id=bc_id)
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
    return channel_configuration()


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
        if connection.kind != "OFFICIAL_API":
            raise DomainError(
                "connection_channel_mismatch", "请使用该连接对应的授权通道"
            )
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


@router.put("/bcs/{bc_id}/default-connection", response_model=FrozenTikTokRoute)
def put_default_connection(
    tenant_id: UUID,
    bc_id: str,
    body: DefaultConnectionRequest,
    session: SessionDep,
    user: CurrentUser,
) -> FrozenTikTokRoute:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    route = set_default_route(
        session,
        context=context,
        bc_id=bc_id,
        connection_id=body.connection_id,
    )
    session.commit()
    return route
