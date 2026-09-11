"""MCP 独立授权入口；回调不依赖当前浏览器租户或 Marketing API App。"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from redis import Redis

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.integrations import CallbackRoute
from app.core.config import settings
from app.core.db import engine
from app.core.errors import ERROR_HTTP_STATUS, DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.bootstrap import candidate_business_centers
from app.integrations.tiktok.mcp_auth.service import (
    accept_mcp_callback,
    cancel_mcp_callback,
    start_mcp_authorization,
    state_attempt,
)
from app.modules.accounts.channel_configuration import mcp_configuration
from app.modules.accounts.connections import (
    bind_candidate_bc,
    disable_connection,
    request_mcp_revocation,
)
from app.modules.accounts.schemas import (
    AuthorizationRequest,
    AuthorizationURL,
    McpBindingRequest,
    McpBindingResult,
    McpCandidateBC,
    McpCandidateBCPage,
    McpConfiguration,
)
from app.modules.tenants.permissions import require_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/tiktok/mcp", tags=["accounts"])
callback_router = APIRouter(
    prefix="/integrations/tiktok/mcp", tags=["integrations"], route_class=CallbackRoute
)


@router.get("/configuration", response_model=McpConfiguration)
def configuration(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> McpConfiguration:
    require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="read")
    return mcp_configuration()


@router.post("/authorizations", response_model=AuthorizationURL)
def authorize(
    tenant_id: UUID,
    body: AuthorizationRequest,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> AuthorizationURL:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    result = start_mcp_authorization(
        session, context=context, connection_id=body.connection_id
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return result


@router.get("/candidates/{attempt_id}/bcs", response_model=McpCandidateBCPage)
def candidate_bcs(
    tenant_id: UUID,
    attempt_id: UUID,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
    page: int = Query(default=1, ge=1, le=100),
    page_size: int = Query(default=50, ge=1, le=100),
) -> McpCandidateBCPage:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    # 当前请求的鉴权事务先结束；后续 HTTP 回调使用独立短 Session。
    session.rollback()
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        items = candidate_business_centers(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            attempt_id=attempt_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    response.headers["Cache-Control"] = "no-store"
    return McpCandidateBCPage(
        items=[
            McpCandidateBC(**item)
            for item in items[(page - 1) * page_size : page * page_size]
        ],
        page=page,
        page_size=page_size,
        total=len(items),
    )


@router.post("/candidates/{attempt_id}/binding", response_model=McpBindingResult)
def binding(
    tenant_id: UUID,
    attempt_id: UUID,
    body: McpBindingRequest,
    session: SessionDep,
    user: CurrentUser,
) -> McpBindingResult:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    run_id = bind_candidate_bc(
        session, context=context, attempt_id=attempt_id, bc_id=body.bc_id
    )
    session.commit()
    return McpBindingResult(discovery_run_id=run_id)


@router.post("/connections/{connection_id}/disable", status_code=204)
def disable(
    tenant_id: UUID, connection_id: UUID, session: SessionDep, user: CurrentUser
) -> Response:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    disable_connection(
        session,
        context=context,
        connection_id=connection_id,
        task_deadline=datetime.now(UTC) + timedelta(seconds=5),
    )
    session.commit()
    return Response(status_code=204)


@router.post("/connections/{connection_id}/revocations")
def revoke(
    tenant_id: UUID, connection_id: UUID, session: SessionDep, user: CurrentUser
) -> dict[str, str]:
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="manage"
    )
    return {
        "task_id": str(
            request_mcp_revocation(
                session, context=context, connection_id=connection_id
            )
        )
    }


@callback_router.get("/callback")
def callback(request: Request) -> Response:
    # 使用原始 query 自行校验，避免 FastAPI 422 把 code/state 回显到验证错误。
    state = request.query_params.get("state", "")
    with bounded_session(
        engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
    ) as session:
        attempt = state_attempt(session, state=state)
        tenant_id, attempt_id = attempt.tenant_id, attempt.id
    try:
        if any(len(request.query_params.getlist(key)) != 1 for key in ("state",)):
            raise DomainError("invalid_oauth_state", "授权回调参数无效")
        issuer = request.query_params.get("iss")
        if issuer is not None and issuer != load_mcp_protocol().issuer:
            raise DomainError("invalid_oauth_state", "授权来源不匹配")
        if request.query_params.get("error"):
            cancel_mcp_callback(database_engine=engine, state=state)
            result = "CANCELLED"
        else:
            if len(request.query_params.getlist("code")) != 1:
                raise DomainError("invalid_auth_code", "授权码无效")
            accept_mcp_callback(
                database_engine=engine,
                state=state,
                code=request.query_params.get("code", ""),
            )
            result = "CANDIDATE_READY"
    except DomainError as error:
        result = error.code if error.code in ERROR_HTTP_STATUS else "internal_error"
    query = urlencode(
        {
            "tab": "connections",
            "mcp_authorization": result,
            "attempt_id": str(attempt_id),
        }
    )
    return RedirectResponse(f"/tenants/{tenant_id}/accounts?{query}", status_code=303)
