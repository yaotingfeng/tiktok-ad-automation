"""One-use, tenant-bound OAuth with encrypted candidate staging.

This service owns transaction commits: claim is durable *before* exchanging a
code, and candidate + outbox commit together afterwards. Call with a dedicated
request Session, never alongside unrelated pending changes. No auth code is
queued or automatically retried after exchange starts.
"""

import json
import multiprocessing
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from multiprocessing.connection import Connection
from time import monotonic
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import business_api_client  # type: ignore[import-untyped]
import business_api_client.tiktok_business.tiktok_exceptions as sdk_errors  # type: ignore[import-untyped]
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import update
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import (
    admitted_account_call,
    checked_data,
    official_client,
)
from app.jobs.admission import AdmissionPolicy
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.models import AuthorizationAttempt, TikTokConnection
from app.modules.tenants.models import AuditEvent
from app.modules.tenants.permissions import require_tenant

TOKEN_ENDPOINT = "/open_api/v1.3/oauth2/access_token/"
OAUTH_DEADLINE_SECONDS = 40
OAUTH_CLEANUP_MARGIN_MS = 10000
register_dispatch_task("accounts.discover", "resources")


def _configured_authorization_url() -> SplitResult:
    settings.require_tiktok_app()
    settings.require_connection_encryption()
    if not settings.TIKTOK_AUTHORIZATION_URL:
        raise DomainError("app_not_configured", "等待配置开发者应用授权地址")
    try:
        base = urlsplit(settings.TIKTOK_AUTHORIZATION_URL)
        valid = (
            base.scheme == "https"
            and base.hostname in {"business-api.tiktok.com", "ads.tiktok.com"}
            and base.username is None
            and base.password is None
            and base.port in {None, 443}
        )
    except ValueError:
        valid = False
    if not valid:
        raise DomainError(
            "invalid_authorization_url", "授权地址必须来自 TikTok 开发者后台"
        )
    return base


def start_authorization(
    session: Session, *, context: TenantContext, connection_id: UUID | None
) -> str:
    context = require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )
    base = _configured_authorization_url()
    connection = (
        session.get(TikTokConnection, connection_id, populate_existing=True)
        if connection_id
        else None
    )
    if connection_id and (
        connection is None or connection.tenant_id != context.tenant_id
    ):
        raise DomainError("connection_not_found", "当前租户连接不存在")
    if connection is None:
        connection = TikTokConnection(tenant_id=context.tenant_id)
        session.add(connection)
        session.flush()
    state = secrets.token_urlsafe(32)
    attempt = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        base_credential_revision=connection.credential_revision,
        state_hash=sha256(state.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(attempt)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.authorization.start",
            target_id=str(connection.id),
            details={"attempt_id": str(attempt.id)},
        )
    )
    session.flush()
    query = dict(parse_qsl(base.query))
    query.update(
        app_id=settings.TIKTOK_APP_ID,
        redirect_uri=settings.TIKTOK_REDIRECT_URI,
        state=state,
    )
    return urlunsplit((base.scheme, base.netloc, base.path, urlencode(query), ""))


def _state_digest(state: str, now: datetime) -> str:
    if not state or len(state) > 512 or now.tzinfo is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    return sha256(state.encode()).hexdigest()


def claim_authorization(
    session: Session, *, state: str, now: datetime
) -> AuthorizationAttempt:
    digest = _state_digest(state, now)
    attempt_id = session.exec(
        update(AuthorizationAttempt)
        .where(
            col(AuthorizationAttempt.state_hash) == digest,
            col(AuthorizationAttempt.claimed_at).is_(None),
            col(AuthorizationAttempt.expires_at) > now,
            col(AuthorizationAttempt.status) == "PENDING",
        )
        .values(claimed_at=now, status="CLAIMED")
        .returning(col(AuthorizationAttempt.id))
    ).scalar_one_or_none()
    if attempt_id is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    loaded_attempt = session.get(
        AuthorizationAttempt, attempt_id, populate_existing=True
    )
    assert loaded_attempt is not None
    attempt = loaded_attempt
    assert attempt is not None
    require_tenant(
        session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id, action="manage"
    )
    return attempt


def _load_attempt(session: Session, attempt_id: UUID) -> AuthorizationAttempt:
    result = session.get(AuthorizationAttempt, attempt_id, populate_existing=True)
    assert result is not None
    return result


def _pending_attempt(
    session: Session, *, state: str, now: datetime
) -> AuthorizationAttempt:
    attempt = session.exec(
        select(AuthorizationAttempt)
        .where(
            col(AuthorizationAttempt.state_hash) == _state_digest(state, now),
            col(AuthorizationAttempt.status) == "PENDING",
            col(AuthorizationAttempt.claimed_at).is_(None),
            col(AuthorizationAttempt.expires_at) > now,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if attempt is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    return attempt


def _token_credentials(data: dict[str, object]) -> dict[str, str]:
    token = data.get("access_token")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise DomainError("invalid_token_response", "授权结果缺少访问凭据")
    result = {"access_token": token}
    if "scope" in data:
        scope = data["scope"]
        if (
            not isinstance(scope, list)
            or len(scope) > 1024
            or any(type(item) is not int or not 0 < item < 2**64 for item in scope)
        ):
            raise DomainError("invalid_token_response", "授权权限范围格式无效")
        # Existing tenant-bound credentials deliberately store string values.
        # Missing scope remains missing; it never implies broad authorization.
        result["scope"] = json.dumps(sorted(set(scope)), separators=(",", ":"))
    return result


def _token_request(*, app_id: str, secret: str, auth_code: str) -> dict[str, str]:
    """Direct official SDK call, executed only inside the bounded OAuth child."""
    with official_client() as client:
        response = business_api_client.AuthenticationApi(client).oauth2_access_token(
            body=business_api_client.Oauth2AccessTokenBody(
                app_id=app_id, secret=secret, auth_code=auth_code
            ),
            _request_timeout=(5, 30),
        )
        return _token_credentials(checked_data(response))


def _exchange_worker(
    channel: Connection, app_id: str, secret: str, auth_code: str
) -> None:
    # Arguments travel through multiprocessing's private pipe, never argv, Redis,
    # logs, or persisted jobs. Return only the token or an application-owned code.
    try:
        token = _token_request(app_id=app_id, secret=secret, auth_code=auth_code)
        channel.send(("ok", token))
    except sdk_errors.TiktokSDKError, ApiException:
        channel.send(("failed", "tiktok_response_error"))
    except DomainError as error:
        channel.send(("failed", error.code))
    except Exception:
        channel.send(("unknown", "oauth_result_unknown"))
    finally:
        channel.close()


def _exchange_token(
    *,
    app_id: str,
    secret: str,
    auth_code: str,
    deadline_seconds: float = OAUTH_DEADLINE_SECONDS,
    _worker: Callable[[Connection, str, str, str], None] = _exchange_worker,
) -> dict[str, str]:
    """Bound the entire generated request, including startup/DNS/body/cleanup.

    Spawn is safe when called by FastAPI's threadpool. A deadline never abandons a
    live request thread: terminate, then kill if necessary, and reap before returning
    so the parent may safely release its admission lease.
    """
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker, args=(send, app_id, secret, auth_code), daemon=True
    )
    deadline = monotonic() + deadline_seconds
    try:
        process.start()
        send.close()
        if not receive.poll(max(0, deadline - monotonic())):
            raise DomainError("oauth_result_unknown", "授权结果未知，请重新发起授权")
        try:
            result = receive.recv()
        except EOFError:
            raise DomainError(
                "oauth_result_unknown", "授权结果未知，请重新发起授权"
            ) from None
        process.join(timeout=max(0, deadline - monotonic()))
        if process.is_alive():
            raise DomainError("oauth_result_unknown", "授权结果未知，请重新发起授权")
        if not isinstance(result, tuple) or len(result) != 2:
            raise DomainError("oauth_result_unknown", "授权结果未知，请重新发起授权")
        status, value = result
        if (
            status == "ok"
            and isinstance(value, dict)
            and set(value) <= {"access_token", "scope"}
        ):
            raw = {"access_token": value.get("access_token")}
            if "scope" in value:
                try:
                    raw["scope"] = json.loads(value["scope"])
                except ValueError, TypeError:
                    raise DomainError(
                        "invalid_token_response", "授权权限范围格式无效"
                    ) from None
            return _token_credentials(raw)
        if status == "failed" and value in {
            "tiktok_response_error",
            "invalid_token_response",
        }:
            raise DomainError(value, "授权兑换失败，请重新发起授权")
        raise DomainError("oauth_result_unknown", "授权结果未知，请重新发起授权")
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        receive.close()
        send.close()


def cancel_authorization(session: Session, *, state: str, now: datetime) -> UUID:
    attempt = claim_authorization(session, state=state, now=now)
    attempt.status = "CANCELLED"
    session.add(attempt)
    session.commit()
    return attempt.connection_id


def finish_authorization(
    session: Session,
    *,
    state: str,
    auth_code: str,
    now: datetime,
    redis_client: Redis,
    policy: AdmissionPolicy,
) -> UUID:
    _configured_authorization_url()
    if not auth_code or len(auth_code) > 4096:
        raise DomainError("invalid_auth_code", "授权码无效")
    if policy.lease_ms <= OAUTH_DEADLINE_SECONDS * 1000 + OAUTH_CLEANUP_MARGIN_MS:
        raise DomainError(
            "admission_policy_invalid", "授权调用租约必须覆盖执行时限和清理余量"
        )
    attempt = _pending_attempt(session, state=state, now=now)
    context = require_tenant(
        session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id, action="manage"
    )
    # Denial leaves state PENDING; exchange failure after claim never replays code.
    admission_started = monotonic()
    with admitted_account_call(
        redis_client,
        context=context,
        endpoint=TOKEN_ENDPOINT,
        advertiser_id="",
        policy=policy,
    ):
        attempt = claim_authorization(
            session, state=state, now=max(now, datetime.now(UTC))
        )
        attempt_id = attempt.id
        session.commit()
        try:
            # Database lock/commit latency consumes the same lease budget.
            remaining_seconds = (policy.lease_ms - OAUTH_CLEANUP_MARGIN_MS) / 1000 - (
                monotonic() - admission_started
            )
            if remaining_seconds <= 0:
                raise DomainError(
                    "oauth_result_unknown", "授权兑换时限已过，请重新发起授权"
                )
            token = _exchange_token(
                app_id=settings.TIKTOK_APP_ID,
                secret=settings.TIKTOK_APP_SECRET,
                auth_code=auth_code,
                deadline_seconds=min(OAUTH_DEADLINE_SECONDS, remaining_seconds),
            )
        except Exception as error:
            code = (
                error.code if isinstance(error, DomainError) else "oauth_result_unknown"
            )
            attempt = _load_attempt(session, attempt_id)
            assert attempt is not None
            attempt.status = (
                "RESULT_UNKNOWN" if code == "oauth_result_unknown" else "FAILED"
            )
            session.add(attempt)
            session.commit()
            raise DomainError(code, "授权兑换未完成，请重新发起授权") from None
    try:
        # Reauthorization retains the current token/version until discovery proves
        # the candidate identities. Nothing here promotes a connection to ACTIVE.
        context = require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="manage",
        )
        attempt = _load_attempt(session, attempt_id)
        assert attempt is not None
        connection = session.exec(
            select(TikTokConnection)
            .where(
                TikTokConnection.id == attempt.connection_id,
                TikTokConnection.tenant_id == context.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if connection.status == "DISABLED":
            raise DomainError("connection_unavailable", "当前租户连接已停用")
        attempt.candidate_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=token
        )
        attempt.status = "CANDIDATE_READY"
        if connection.credential_ciphertext is None:
            connection.status = "DISCOVERING"
        session.add_all([attempt, connection])
        enqueue_after_commit(
            session,
            context=context,
            task_name="accounts.discover",
            task_key=f"authorization:{attempt.id}:discover",
            payload={"attempt_id": str(attempt.id)},
        )
        session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                action="tiktok.authorization.candidate_ready",
                target_id=str(connection.id),
                details={"attempt_id": str(attempt.id)},
            )
        )
        session.commit()
        return connection.id
    except Exception:
        session.rollback()
        attempt = _load_attempt(session, attempt_id)
        assert attempt is not None
        attempt.status = "FAILED"
        attempt.candidate_ciphertext = None
        session.add(attempt)
        session.commit()
        raise
