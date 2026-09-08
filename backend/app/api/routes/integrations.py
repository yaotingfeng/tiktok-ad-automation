from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from redis import Redis
from sqlmodel import select

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import ERROR_HTTP_STATUS, DomainError, domain_error_handler
from app.integrations.tiktok import auth
from app.jobs.admission import admission_policy
from app.modules.accounts.models import AuthorizationAttempt


class CallbackRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def private_response(request: Request) -> Response:
            try:
                response = await original(request)
            except DomainError as error:
                response = await domain_error_handler(request, error)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        return private_response


router = APIRouter(
    prefix="/integrations", tags=["integrations"], route_class=CallbackRoute
)


@router.get("/tiktok/callback")
def tiktok_callback(request: Request, session: SessionDep) -> Response:
    """Restore authorization scope from state; never trust current browser tenant.

    This route has no CurrentUser dependency: its sole Session is dedicated to
    finish_authorization's durable state claim and candidate transactions.
    Raw callback parameters are not Pydantic fields or echoed validation inputs.
    """
    settings.require_tiktok_app()
    state = request.query_params.get("state", "")
    if not state or len(state) > 512:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    attempt = session.exec(
        select(AuthorizationAttempt).where(
            AuthorizationAttempt.state_hash == sha256(state.encode()).hexdigest()
        )
    ).one_or_none()
    if attempt is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    tenant_id, connection_id = attempt.tenant_id, attempt.connection_id
    now = datetime.now(UTC)
    try:
        if request.query_params.get("error"):
            connection_id = auth.cancel_authorization(session, state=state, now=now)
            status = "CANCELLED"
        else:
            # Check consumed/expired state before configuration or quota work.
            if (
                attempt.status != "PENDING"
                or attempt.claimed_at
                or attempt.expires_at <= now
            ):
                raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
            with Redis.from_url(
                settings.REDIS_URL, decode_responses=True
            ) as redis_client:
                connection_id = auth.finish_authorization(
                    session,
                    state=state,
                    auth_code=request.query_params.get("auth_code", ""),
                    now=now,
                    redis_client=redis_client,
                    policy=admission_policy(auth.TOKEN_ENDPOINT),
                )
            status = "CANDIDATE_READY"
    except DomainError as error:
        session.rollback()
        # Do not remove an unconsumed code from the browser when admission is
        # deferred: the same callback can be retried while state remains valid.
        if error.code.startswith("admission_") or error.code in {
            "app_not_configured",
            "tiktok_app_not_configured",
            "tiktok_app_incomplete",
            "invalid_authorization_url",
            "connection_encryption_unconfigured",
        }:
            raise
        status = error.code if error.code in ERROR_HTTP_STATUS else "internal_error"
    query = urlencode(
        {
            "tab": "connections",
            "authorization": status,
            "connection_id": str(connection_id),
        }
    )
    return RedirectResponse(f"/tenants/{tenant_id}/accounts?{query}", status_code=303)
