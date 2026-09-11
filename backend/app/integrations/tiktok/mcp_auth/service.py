"""租户 MCP OAuth：一次性 claim、PKCE、加密候选；此处不发布当前连接。"""

import base64
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.mcp.protocol import McpProtocolProfile, load_mcp_protocol
from app.integrations.tiktok.mcp_auth.transport import exchange_token, strict_json
from app.modules.accounts.connection_models import McpAuthorizationAttempt
from app.modules.accounts.models import TikTokConnection
from app.modules.accounts.schemas import AuthorizationURL
from app.modules.tenants.models import AuditEvent, TenantMembership
from app.modules.tenants.permissions import require_tenant

OAUTH_SECONDS = 30


@dataclass(frozen=True, repr=False)
class Registration:
    client_id: str
    redirect_uri: str


def load_registration(profile: McpProtocolProfile) -> Registration:
    """部署材料只能来自有界本地普通文件；绝不从浏览器/网络取得客户端信息。"""
    profile.require_authorization_verified()
    settings.require_connection_encryption()
    ref = settings.MCP_CLIENT_REGISTRATION_REF
    if not ref:
        raise DomainError("mcp_client_unregistered", "MCP 客户端尚未注册")
    redirect = settings.MCP_REDIRECT_URI
    failed = False
    try:
        parsed = urlsplit(redirect)
        if not (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path == "/api/integrations/tiktok/mcp/callback"
            and "\\" not in redirect
            and len(redirect) <= 2048
        ):
            raise ValueError("invalid redirect")
        if not os.path.isabs(ref):
            raise ValueError("expected local absolute registration path")
        descriptor = os.open(ref, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as material:
            info = os.fstat(material.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("invalid registration material")
            raw = material.read(16385)
            if len(raw) > 16384:
                raise ValueError("registration too large")
        data = strict_json(raw)
        if set(data) != {
            "client_id",
            "issuer",
            "resource",
            "token_endpoint_auth_method",
            "redirect_uris",
        }:
            raise ValueError("unexpected registration fields")
        client_id = data["client_id"]
        redirects = data["redirect_uris"]
        if not (
            type(client_id) is str
            and 0 < len(client_id) <= 512
            and all(32 < ord(c) < 127 for c in client_id)
            and data["issuer"] == profile.issuer
            and data["resource"] == profile.resource
            and data["token_endpoint_auth_method"] == "none"
            and "none" in (profile.token_auth_methods or ())
            and type(redirects) is list
            and len(redirects) <= 20
            and all(type(uri) is str and len(uri) <= 2048 for uri in redirects)
            and redirect in redirects
        ):
            raise ValueError("registration mismatch")
    except OSError, ValueError, TypeError, KeyError:
        failed = True
    if failed:
        raise DomainError("mcp_client_registration_invalid", "MCP 客户端注册配置无效")
    return Registration(client_id=client_id, redirect_uri=redirect)


def _connection(
    session: Session, *, tenant_id: UUID, connection_id: UUID
) -> TikTokConnection:
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == connection_id,
            TikTokConnection.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None or connection.kind != "OFFICIAL_MCP":
        raise DomainError("connection_not_found", "当前租户 MCP 连接不存在")
    if connection.status == "DISABLED":
        raise DomainError("connection_unavailable", "连接已停用")
    return connection


def _check_parent(
    attempt: McpAuthorizationAttempt, connection: TikTokConnection
) -> None:
    if (attempt.base_credential_revision, attempt.base_authorization_revision) != (
        connection.credential_revision,
        connection.authorization_revision,
    ):
        raise DomainError("mcp_candidate_superseded", "候选授权已被后续授权替代")


def require_mcp_admin(
    session: Session, *, actor_id: UUID, tenant_id: UUID
) -> TenantContext:
    """MCP 候选授权始终需要当前 manage 权限和有效租户成员关系。"""
    context = require_tenant(
        session, actor_id=actor_id, tenant_id=tenant_id, action="manage"
    )
    # 超级管理员的通用平台管理权限不能替代本租户授权所需的有效成员关系。
    # 该检查也用于回调和逐次候选 HTTP，不沿用发起授权时的历史成员状态。
    member = session.get(
        TenantMembership, (tenant_id, actor_id), populate_existing=True
    )
    if member is None or not member.active:
        raise DomainError("action_forbidden", "请由该租户的有效管理员操作 MCP 授权")
    return context


def start_mcp_authorization(
    session: Session, *, context: TenantContext, connection_id: UUID | None
) -> AuthorizationURL:
    context = require_mcp_admin(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id
    )
    profile = load_mcp_protocol()
    registration = load_registration(profile)
    # load_registration 已核实授权协议；保留明确的类型边界，不将缺失值写入候选。
    assert profile.issuer is not None and profile.resource is not None
    connection = (
        _connection(session, tenant_id=context.tenant_id, connection_id=connection_id)
        if connection_id
        else None
    )
    if connection is None:
        connection = TikTokConnection(
            tenant_id=context.tenant_id,
            kind="OFFICIAL_MCP",
            service_profile=profile.revision,
            adapter_contract_revision=profile.schema_manifest_sha256,
        )
        session.add(connection)
        session.flush()
    # 新尝试优先；旧 claim 的迟到响应只能保存失败，不能覆盖新候选。
    for older in session.exec(
        select(McpAuthorizationAttempt).where(
            McpAuthorizationAttempt.tenant_id == context.tenant_id,
            McpAuthorizationAttempt.connection_id == connection.id,
            col(McpAuthorizationAttempt.status).in_(
                ["PENDING", "CLAIMED", "CANDIDATE_READY"]
            ),
        )
    ).all():
        older.status = "CANCELLED"
        older.pkce_verifier_ciphertext = None
        older.candidate_ciphertext = None
        older.completed_at = datetime.now(UTC)
        session.add(older)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    attempt = McpAuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        base_credential_revision=connection.credential_revision,
        base_authorization_revision=connection.authorization_revision,
        issuer=profile.issuer,
        resource=profile.resource,
        redirect_uri=registration.redirect_uri,
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        pkce_verifier_ciphertext=encrypt_credentials(
            tenant_id=context.tenant_id,
            value={
                "verifier": verifier,
                "client_id": registration.client_id,
            },
        ),
    )
    session.add(attempt)
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.authorization.start",
            target_id=str(connection.id),
            details={"attempt_id": str(attempt.id)},
        )
    )
    session.flush()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return AuthorizationURL(
        url=f"{profile.authorization_endpoint}?{
            urlencode(
                {
                    'response_type': 'code',
                    'client_id': registration.client_id,
                    'redirect_uri': registration.redirect_uri,
                    'resource': profile.resource,
                    'scope': ' '.join(profile.scopes_supported or ()),
                    'state': state,
                    'code_challenge': challenge,
                    'code_challenge_method': 'S256',
                }
            )
        }"
    )


def state_attempt(session: Session, *, state: str) -> McpAuthorizationAttempt:
    if type(state) is not str or not 0 < len(state) <= 512:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    attempt = session.exec(
        select(McpAuthorizationAttempt)
        .where(
            McpAuthorizationAttempt.state_hash
            == hashlib.sha256(state.encode()).hexdigest(),
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if attempt is None:
        raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
    return attempt


def _credentials(
    data: dict[str, Any], *, profile: McpProtocolProfile
) -> dict[str, str]:
    invalid = False
    token = data.get("access_token")
    if not (
        type(token) is str
        and 0 < len(token) <= 16384
        and all(32 < ord(c) < 127 for c in token)
    ):
        invalid = True
    if (
        type(data.get("token_type")) is not str
        or data["token_type"].lower() != "bearer"
    ):
        invalid = True
    for key, expected in (
        ("issuer", profile.issuer),
        ("iss", profile.issuer),
        ("resource", profile.resource),
        ("aud", profile.resource),
    ):
        if key in data and data[key] != expected:
            invalid = True
    scope = data.get("scope")
    scopes = []
    if scope is not None:
        if type(scope) is not str or len(scope) > 4096:
            invalid = True
        else:
            scopes = sorted(set(scope.split()))
            if not set(scopes) <= set(profile.scopes_supported or ()):
                invalid = True
    refresh = data.get("refresh_token")
    if refresh is not None and not (
        type(refresh) is str
        and 0 < len(refresh) <= 16384
        and all(32 < ord(c) < 127 for c in refresh)
    ):
        invalid = True
    expires = data.get("expires_in")
    if expires is not None and (
        type(expires) is not int or not 0 < expires <= 10 * 365 * 86400
    ):
        invalid = True
    if invalid:
        raise DomainError("mcp_token_response_invalid", "MCP 授权凭据格式无效")
    assert isinstance(token, str)
    assert profile.issuer is not None and profile.resource is not None
    value: dict[str, str] = {
        "access_token": token,
        "token_type": "Bearer",
        "scopes": json.dumps(scopes),
        "issuer": profile.issuer,
        "resource": profile.resource,
        "received_at": datetime.now(UTC).isoformat(),
    }
    if refresh is not None:
        value["refresh_token"] = refresh
    if expires is not None:
        value["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=expires)
        ).isoformat()
    # 未返回 subject/grant 就保持未知，不能从 JWT/工具名推断授权主体。
    return value


def accept_mcp_callback(*, database_engine: Engine, state: str, code: str) -> UUID:
    if (
        type(code) is not str
        or not 0 < len(code) <= 4096
        or any(ord(c) < 32 for c in code)
    ):
        raise DomainError("invalid_auth_code", "授权码无效")
    deadline = datetime.now(UTC) + timedelta(seconds=OAUTH_SECONDS)
    profile = load_mcp_protocol()
    registration = load_registration(profile)
    assert profile.token_endpoint is not None and profile.resource is not None
    claim_id = uuid4()
    with bounded_session(database_engine, task_deadline=deadline) as session:
        attempt = state_attempt(session, state=state)
        connection = _connection(
            session, tenant_id=attempt.tenant_id, connection_id=attempt.connection_id
        )
        session.refresh(attempt)
        now = datetime.now(UTC)
        if attempt.status != "PENDING" or attempt.expires_at <= now:
            raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
        require_mcp_admin(
            session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id
        )
        _check_parent(attempt, connection)
        if (attempt.issuer, attempt.resource, attempt.redirect_uri) != (
            profile.issuer,
            profile.resource,
            registration.redirect_uri,
        ):
            raise DomainError("mcp_client_registration_invalid", "MCP 注册配置已变化")
        material = decrypt_credentials(
            tenant_id=attempt.tenant_id,
            ciphertext=attempt.pkce_verifier_ciphertext or "",
        )
        if material.get("client_id") != registration.client_id:
            raise DomainError("mcp_client_registration_invalid", "MCP 注册配置已变化")
        attempt.status = "CLAIMED"
        attempt.claim_id = claim_id
        attempt.claimed_at = now
        attempt.claimed_until = deadline
        attempt_id, tenant_id = attempt.id, attempt.tenant_id
        session.add(attempt)
        # CLAIMED 是 authorization code 的发送闩锁；超时也不能恢复 PENDING 重放。
        session.commit()
    failure = None
    try:
        data = exchange_token(
            endpoint=profile.token_endpoint,
            form={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": material["verifier"],
                "client_id": registration.client_id,
                "redirect_uri": registration.redirect_uri,
                "resource": profile.resource,
            },
            task_deadline=deadline,
        )
        credentials = _credentials(data, profile=profile)
        # 刷新必须沿用发放此授权的客户端，不能仅相信日后更换的部署配置。
        credentials["client_id"] = registration.client_id
        ciphertext = encrypt_credentials(tenant_id=tenant_id, value=credentials)
    except Exception as exc:
        failure = (
            exc.code if isinstance(exc, DomainError) else "mcp_oauth_result_unknown"
        )
    # 清理/保存使用独立短预算；它不能重启已结束的 HTTP 预算或重新兑换 code。
    with bounded_session(
        database_engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
    ) as session:
        loaded = session.get(McpAuthorizationAttempt, attempt_id)
        if loaded is None:
            raise DomainError("mcp_candidate_unavailable", "候选授权不可用")
        attempt = loaded
        connection = _connection(
            session, tenant_id=tenant_id, connection_id=attempt.connection_id
        )
        session.refresh(attempt)
        if attempt.status != "CLAIMED" or attempt.claim_id != claim_id:
            raise DomainError("mcp_candidate_superseded", "候选授权已被后续授权替代")
        if failure is None:
            try:
                require_mcp_admin(
                    session, actor_id=attempt.actor_id, tenant_id=tenant_id
                )
                _check_parent(attempt, connection)
                if datetime.now(UTC) > deadline or attempt.expires_at <= datetime.now(
                    UTC
                ):
                    raise DomainError("mcp_oauth_result_unknown", "授权结果迟到")
            except DomainError as exc:
                failure = exc.code
        attempt.pkce_verifier_ciphertext = None
        attempt.completed_at = datetime.now(UTC)
        if failure:
            attempt.status = (
                "RESULT_UNKNOWN" if failure == "mcp_oauth_result_unknown" else "FAILED"
            )
            attempt.error_code = failure
        else:
            attempt.candidate_ciphertext = ciphertext
            attempt.status = "CANDIDATE_READY"
        session.add(attempt)
        session.commit()
    if failure:
        raise DomainError(failure, "授权候选未就绪，请重新发起授权")
    return attempt_id


def cancel_mcp_callback(*, database_engine: Engine, state: str) -> UUID:
    with bounded_session(
        database_engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
    ) as session:
        attempt = state_attempt(session, state=state)
        _connection(
            session, tenant_id=attempt.tenant_id, connection_id=attempt.connection_id
        )
        session.refresh(attempt)
        if attempt.status != "PENDING" or attempt.expires_at <= datetime.now(UTC):
            raise DomainError("invalid_oauth_state", "授权回调已失效或已使用")
        require_mcp_admin(
            session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id
        )
        attempt.status = "CANCELLED"
        attempt.pkce_verifier_ciphertext = None
        attempt.completed_at = datetime.now(UTC)
        session.add(attempt)
        session.commit()
        return attempt.id
