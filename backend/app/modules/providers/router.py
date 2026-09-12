"""Tenant provider API. Validation failures never reflect submitted credentials."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.errors import ERROR_HTTP_STATUS, DomainError
from app.core.pagination import Page
from app.modules.providers.catalog import (
    LinkStatus,
    get_link,
    list_links,
    preparation_summary,
)
from app.modules.providers.connections import (
    save_connection,
    set_application_minis,
    verify_connection,
)
from app.modules.providers.models import ProviderApplication, ProviderConnection
from app.modules.providers.repository import _after_id, _cursor, get_connection
from app.modules.providers.schemas import (
    CandidateSelection,
    LinkPreparationRequest,
    LinkResultStatus,
    PreparationAccepted,
    PreparationSummary,
    ProviderApplicationPublic,
    ProviderConnectionCreate,
    ProviderConnectionPublic,
    ProviderConnectionUpdate,
    ProviderKind,
    ProviderLinkPublic,
    ProviderMinisUpdate,
    ResolvedLink,
)
from app.modules.providers.service import (
    choose_drama_candidate,
    get_link_results,
    prepare_links,
)
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant


class ProviderRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "provider_request_invalid",
                        "message": "版权方输入无效",
                        "retryable": False,
                    },
                )

        return safe_handler


router = APIRouter(
    prefix="/tenants/{tenant_id}/providers",
    tags=["providers"],
    route_class=ProviderRoute,
)
Cursor = Annotated[str | None, Query(max_length=4096)]
Limit = Annotated[int, Query(ge=1, le=100)]


def _public(connection: ProviderConnection) -> ProviderConnectionPublic:
    result = ProviderConnectionPublic.model_validate(connection)
    if result.error_code not in ERROR_HTTP_STATUS:
        result.error_code = "provider_unavailable" if result.error_code else None
    return result


@router.post("/link-preparations", response_model=PreparationAccepted, status_code=202)
def post_preparation(
    tenant_id: UUID,
    body: LinkPreparationRequest,
    session: SessionDep,
    user: CurrentUser,
) -> PreparationAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="provider_write"
    )
    task_id = prepare_links(session, context=context, **body.model_dump())
    session.commit()
    return PreparationAccepted(task_id=task_id)


@router.get("/link-preparations/{task_id}", response_model=Page[ResolvedLink])
def get_preparation(
    tenant_id: UUID,
    task_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    page_size: int = 100,
    status: LinkResultStatus | None = None,
    exceptions_only: bool = False,
) -> Page[ResolvedLink]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return get_link_results(
        session,
        context=context,
        task_id=task_id,
        cursor=cursor,
        page_size=page_size,
        status=status,
        exceptions_only=exceptions_only,
    )


@router.get("/link-preparations/{task_id}/summary", response_model=PreparationSummary)
def get_preparation_summary(
    tenant_id: UUID, task_id: UUID, session: SessionDep, user: CurrentUser
) -> PreparationSummary:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return preparation_summary(session, context=context, task_id=task_id)


@router.get("/links", response_model=Page[ProviderLinkPublic])
def get_links(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    connection_id: UUID | None = None,
    application_id: Annotated[str | None, Query(max_length=255)] = None,
    query: Annotated[str, Query(max_length=1000)] = "",
    status: LinkStatus | None = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[ProviderLinkPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return list_links(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
        query=query,
        status=status,
        cursor=cursor,
        limit=limit,
    )


@router.get("/links/{link_id}", response_model=ProviderLinkPublic)
def link_details(
    tenant_id: UUID, link_id: UUID, session: SessionDep, user: CurrentUser
) -> ProviderLinkPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    return get_link(session, context=context, link_id=link_id)


@router.post(
    "/inputs/{input_id}/candidate", response_model=PreparationAccepted, status_code=202
)
def post_candidate(
    tenant_id: UUID,
    input_id: UUID,
    body: CandidateSelection,
    session: SessionDep,
    user: CurrentUser,
) -> PreparationAccepted:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="provider_write"
    )
    task_id = choose_drama_candidate(
        session,
        context=context,
        input_id=input_id,
        external_drama_id=body.external_drama_id,
    )
    session.commit()
    return PreparationAccepted(task_id=task_id)


@router.get("/connections", response_model=Page[ProviderConnectionPublic])
def list_connections(
    tenant_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
    query: Annotated[str, Query(max_length=255)] = "",
    kind: ProviderKind | None = None,
    status: Literal[
        "pending", "verifying", "active", "reauth_required", "error", "disabled"
    ]
    | None = None,
) -> Page[ProviderConnectionPublic]:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    scope = {"tenant_id": str(tenant_id), "kind": "provider-connections"}
    if query:
        scope["query"] = query
    if kind:
        scope["provider_kind"] = kind
    if status:
        scope["status"] = status
    last_id = _after_id(cursor, scope)
    statement = select(ProviderConnection).where(
        ProviderConnection.tenant_id == tenant_id
    )
    if query.strip():
        statement = statement.where(
            col(ProviderConnection.display_name).icontains(
                query.strip(), autoescape=True
            )
        )
    if kind:
        statement = statement.where(ProviderConnection.kind == kind)
    if status:
        statement = statement.where(ProviderConnection.status == status)
    if last_id is not None:
        statement = statement.where(col(ProviderConnection.id) > last_id)
    rows = session.exec(
        statement.order_by(col(ProviderConnection.id)).limit(limit + 1)
    ).all()
    return Page(
        items=[_public(row) for row in rows[:limit]],
        next_cursor=_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.post("/connections", response_model=ProviderConnectionPublic, status_code=201)
def post_connection(
    tenant_id: UUID,
    body: ProviderConnectionCreate,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> ProviderConnectionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = save_connection(session, context=context, **body.model_dump())
    result = _public(row)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/connections/{connection_id}", response_model=ProviderConnectionPublic)
def patch_connection(
    tenant_id: UUID,
    connection_id: UUID,
    body: ProviderConnectionUpdate,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> ProviderConnectionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = session.exec(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.id == connection_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "当前租户版权方连接不存在")
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    if not body.model_fields_set or (
        body.credentials is not None and body.status is not None
    ):
        raise DomainError("provider_request_invalid", "连接更新请求无效")
    if body.credentials is not None:
        row = save_connection(
            session,
            context=context,
            kind=row.kind,
            connection_id=row.id,
            display_name=body.display_name or row.display_name,
            credentials=body.credentials,
        )
    else:
        if body.display_name is not None:
            if not body.display_name.strip():
                raise DomainError("provider_request_invalid", "连接名称不能为空")
            row.display_name = body.display_name.strip()
        if body.status is not None:
            row.status = body.status
        session.add(row)
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=user.id,
                action="provider.connection.update",
                target_id=str(row.id),
            )
        )
    result = _public(row)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/connections/{connection_id}/verify", response_model=ProviderConnectionPublic
)
def post_verify(
    tenant_id: UUID, connection_id: UUID, session: SessionDep, user: CurrentUser
) -> ProviderConnectionPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    get_connection(session, context=context, connection_id=connection_id)
    bind = session.get_bind()
    database_engine = getattr(bind, "engine", bind)
    session.commit()
    verify_connection(
        database_engine=database_engine, context=context, connection_id=connection_id
    )
    return _public(
        get_connection(session, context=context, connection_id=connection_id)
    )


@router.get(
    "/connections/{connection_id}/applications",
    response_model=Page[ProviderApplicationPublic],
)
def list_applications(
    tenant_id: UUID,
    connection_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> Page[ProviderApplicationPublic]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    connection = get_connection(session, context=context, connection_id=connection_id)
    scope = {
        "tenant_id": str(tenant_id),
        "connection_id": str(connection_id),
        "kind": "provider-applications",
    }
    last_id = _after_id(cursor, scope)
    statement = select(ProviderApplication).where(
        ProviderApplication.tenant_id == tenant_id,
        ProviderApplication.connection_id == connection_id,
    )
    if last_id is not None:
        statement = statement.where(col(ProviderApplication.id) > last_id)
    rows = session.exec(
        statement.order_by(col(ProviderApplication.id)).limit(limit + 1)
    ).all()
    return Page(
        items=[
            ProviderApplicationPublic(
                external_id=row.external_id,
                name=row.name,
                tiktok_minis_id=row.tiktok_minis_id,
                available=connection.status in {"active", "reauth_required"}
                and connection.verification_token is not None
                and row.channel_config.get("verification_token")
                == str(connection.verification_token),
            )
            for row in rows[:limit]
        ],
        next_cursor=_cursor(scope, rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.patch(
    "/connections/{connection_id}/applications/{application_id}/minis",
    response_model=ProviderApplicationPublic,
)
def update_application_minis(
    tenant_id: UUID,
    connection_id: UUID,
    application_id: str,
    body: ProviderMinisUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> ProviderApplicationPublic:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    row = set_application_minis(
        session,
        context=context,
        connection_id=connection_id,
        application_id=application_id,
        minis_id=body.minis_id,
    )
    result = ProviderApplicationPublic(
        external_id=row.external_id,
        name=row.name,
        tiktok_minis_id=row.tiktok_minis_id,
        available=True,
    )
    session.commit()
    return result
