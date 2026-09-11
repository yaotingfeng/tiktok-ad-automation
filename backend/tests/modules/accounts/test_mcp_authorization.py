import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.service import start_mcp_authorization
from app.modules.accounts.connection_models import McpAuthorizationAttempt
from tests.modules.conftest import create_context


@pytest.fixture(autouse=True)
def app_config(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    profile = load_mcp_protocol()
    redirect = "https://tkada.example.test/api/integrations/tiktok/mcp/callback"
    registration = tmp_path / "registration.json"
    registration.write_text(
        json.dumps(
            {
                "client_id": "synthetic-client",
                "issuer": profile.issuer,
                "resource": profile.resource,
                "token_endpoint_auth_method": "none",
                "redirect_uris": [redirect],
            }
        )
    )
    monkeypatch.setattr(settings, "MCP_CLIENT_REGISTRATION_REF", str(registration))
    monkeypatch.setattr(settings, "MCP_REDIRECT_URI", redirect)


def test_api_app_absence_does_not_block_mcp(session):
    context = create_context(session, role="tenant_admin")
    result = start_mcp_authorization(session, context=context, connection_id=None)
    assert urlsplit(result.url).hostname == "business-api.tiktok.com"
    query = parse_qs(urlsplit(result.url).query)
    assert query["code_challenge_method"] == ["S256"]
    attempt = session.exec(
        select(McpAuthorizationAttempt).where(
            McpAuthorizationAttempt.tenant_id == context.tenant_id
        )
    ).one()
    assert attempt.pkce_verifier_ciphertext
    assert query["state"][0] not in repr(attempt)
    assert query["state"][0] != attempt.state_hash


def test_missing_registration_is_recognizable(session, monkeypatch):
    context = create_context(session, role="tenant_admin")
    monkeypatch.setattr(settings, "MCP_CLIENT_REGISTRATION_REF", "")
    with pytest.raises(DomainError) as error:
        start_mcp_authorization(session, context=context, connection_id=None)
    assert error.value.code == "mcp_client_unregistered"


def test_operator_cannot_authorize(session):
    context = create_context(session)
    with pytest.raises(DomainError) as error:
        start_mcp_authorization(session, context=context, connection_id=None)
    assert error.value.code == "action_forbidden"


@pytest.fixture
def committed_context():
    from sqlmodel import Session, delete

    from app.core.db import engine
    from app.jobs.models import DispatchTenantCursor, PendingDispatch
    from app.models import User
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
        ConnectionToolObservation,
    )
    from app.modules.accounts.models import DiscoveryRun, TenantBC, TikTokConnection
    from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership

    with Session(engine) as own:
        context = create_context(own, role="tenant_admin")
        own.commit()
    try:
        yield context
    finally:
        with Session(engine) as own:
            for model in (
                PendingDispatch,
                DispatchTenantCursor,
                AuditEvent,
                DiscoveryRun,
                ConnectionToolObservation,
                BCDefaultRoute,
                BCConnectionBinding,
                McpAuthorizationAttempt,
                TikTokConnection,
                TenantBC,
                TenantMembership,
            ):
                own.exec(delete(model).where(model.tenant_id == context.tenant_id))
            own.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            own.exec(delete(User).where(User.id == context.actor_id))
            own.commit()


@pytest.fixture
def oauth_wire(monkeypatch):
    import httpx2

    from app.integrations.tiktok.mcp_auth import transport

    class Wire:
        calls = None
        before = None
        status = 200
        headers = {"content-type": "application/json"}
        data = {
            "access_token": "synthetic-access-secret",
            "refresh_token": "synthetic-refresh-secret",
            "token_type": "Bearer",
            "scope": "mcp:tt4b",
            "expires_in": 3600,
        }

        def __init__(self):
            self.calls = []

        async def request(self, request):
            assert str(request.url) == load_mcp_protocol().token_endpoint
            assert request.method == "POST"
            self.calls.append(parse_qs(request.content.decode()))
            if self.before:
                self.before()
            return httpx2.Response(self.status, json=self.data, headers=self.headers)

    wire = Wire()
    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(wire.request)
    )
    return wire


def issue(context, connection_id=None):
    from sqlmodel import Session

    from app.core.db import engine

    with Session(engine) as own:
        url = start_mcp_authorization(own, context=context, connection_id=connection_id)
        state = parse_qs(urlsplit(url.url).query)["state"][0]
        attempt = own.exec(
            select(McpAuthorizationAttempt)
            .where(McpAuthorizationAttempt.tenant_id == context.tenant_id)
            .order_by(McpAuthorizationAttempt.created_at.desc())
        ).first()
        identity = attempt.id
        own.commit()
    return state, identity


def accept(state):
    from app.core.db import engine
    from app.integrations.tiktok.mcp_auth.service import accept_mcp_callback

    return accept_mcp_callback(
        database_engine=engine, state=state, code="synthetic-private-code"
    )


def test_claim_is_durable_before_exchange_and_replay_blocked(
    committed_context, oauth_wire
):
    from sqlmodel import Session

    from app.core.credentials import decrypt_credentials
    from app.core.db import engine

    state, identity = issue(committed_context)

    def before():
        with Session(engine) as own:
            row = own.get(McpAuthorizationAttempt, identity)
            assert row.status == "CLAIMED" and row.claimed_at and row.claim_id
            assert row.candidate_ciphertext is None

    oauth_wire.before = before
    assert accept(state) == identity
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, identity)
        assert attempt.status == "CANDIDATE_READY"
        assert attempt.pkce_verifier_ciphertext is None
        assert "synthetic-access-secret" not in attempt.candidate_ciphertext
        material = decrypt_credentials(
            tenant_id=committed_context.tenant_id,
            ciphertext=attempt.candidate_ciphertext,
        )
        assert material["access_token"] == "synthetic-access-secret"
        assert material["client_id"] == "synthetic-client"
    with pytest.raises(DomainError) as error:
        accept(state)
    assert error.value.code == "invalid_oauth_state"
    assert len(oauth_wire.calls) == 1
    assert oauth_wire.calls[0]["resource"] == [load_mcp_protocol().resource]
    assert len(oauth_wire.calls[0]["code_verifier"][0]) >= 43


@pytest.mark.parametrize("change", ["role", "expired", "state"])
def test_unavailable_authorization_never_posts(committed_context, oauth_wire, change):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.tenants.models import TenantMembership

    state, identity = issue(committed_context)
    with Session(engine) as own:
        if change == "role":
            row = own.get(
                TenantMembership,
                (committed_context.tenant_id, committed_context.actor_id),
            )
            row.role = "operator"
        else:
            row = own.get(McpAuthorizationAttempt, identity)
            if change == "expired":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        own.add(row)
        own.commit()
    with pytest.raises(DomainError):
        accept("not-issued" if change == "state" else state)
    assert oauth_wire.calls == []


@pytest.mark.parametrize("change", ["new_attempt", "disable", "role"])
def test_late_result_cannot_overwrite_current_authorization(
    committed_context, oauth_wire, change
):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.connections import disable_connection
    from app.modules.accounts.models import TikTokConnection
    from app.modules.tenants.models import TenantMembership

    state, identity = issue(committed_context)
    with Session(engine) as own:
        connection_id = own.get(McpAuthorizationAttempt, identity).connection_id
        connection = own.get(TikTokConnection, connection_id)
        connection.status = "ACTIVE"
        connection.credential_ciphertext = "old-encrypted-material"
        own.add(connection)
        own.commit()

    def before():
        if change == "new_attempt":
            issue(committed_context, connection_id=connection_id)
        else:
            with Session(engine) as own:
                if change == "disable":
                    disable_connection(
                        own,
                        context=committed_context,
                        connection_id=connection_id,
                        task_deadline=datetime.now(UTC) + timedelta(seconds=5),
                    )
                else:
                    row = own.get(
                        TenantMembership,
                        (committed_context.tenant_id, committed_context.actor_id),
                    )
                    row.role = "operator"
                    own.add(row)
                own.commit()

    oauth_wire.before = before
    with pytest.raises(DomainError):
        accept(state)
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, identity)
        connection = own.get(TikTokConnection, connection_id)
        assert attempt.candidate_ciphertext is None
        assert connection.credential_ciphertext == "old-encrypted-material"
        assert connection.status == ("DISABLED" if change == "disable" else "ACTIVE")
    assert len(oauth_wire.calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"access_token": "synthetic-access-secret", "token_type": "Basic"},
        {
            "access_token": "synthetic-access-secret",
            "token_type": "Bearer",
            "resource": "https://wrong.invalid",
        },
        {
            "access_token": "synthetic-access-secret",
            "token_type": "Bearer",
            "scope": "other",
        },
        {
            "access_token": "synthetic-access-secret",
            "token_type": "Bearer",
            "expires_in": True,
        },
    ],
)
def test_token_semantics_reject_without_secret_chains(
    committed_context, oauth_wire, data
):
    state, _ = issue(committed_context)
    oauth_wire.data = data
    with pytest.raises(DomainError) as error:
        accept(state)
    assert error.value.code == "mcp_token_response_invalid"
    assert error.value.__context__ is None
    assert "synthetic-access-secret" not in str(error.value)
    with pytest.raises(DomainError):
        accept(state)
    assert len(oauth_wire.calls) == 1


def test_callback_private_redirect_and_replay(client, committed_context, oauth_wire):
    state, identity = issue(committed_context)
    response = client.get(
        "/api/integrations/tiktok/mcp/callback",
        params={
            "state": state,
            "code": "synthetic-private-code",
            "tenant_id": "other-tenant",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        f"/tenants/{committed_context.tenant_id}/accounts?"
    )
    assert str(identity) in response.headers["location"]
    assert "CANDIDATE_READY" in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "synthetic-private-code" not in str(response.headers) + response.text
    assert state not in str(response.headers)
    again = client.get(
        "/api/integrations/tiktok/mcp/callback",
        params={"state": state, "code": "synthetic-private-code"},
        follow_redirects=False,
    )
    assert "invalid_oauth_state" in again.headers["location"]
    assert len(oauth_wire.calls) == 1


def test_callback_cancellation_and_issuer_mismatch(
    client, committed_context, oauth_wire
):
    state, identity = issue(committed_context)
    mismatch = client.get(
        "/api/integrations/tiktok/mcp/callback",
        params={
            "state": state,
            "code": "synthetic-private-code",
            "iss": "https://evil.invalid",
        },
        follow_redirects=False,
    )
    assert "invalid_oauth_state" in mismatch.headers["location"]
    cancelled = client.get(
        "/api/integrations/tiktok/mcp/callback",
        params={
            "state": state,
            "error": "access_denied",
            "error_description": "synthetic-secret",
        },
        follow_redirects=False,
    )
    assert "CANCELLED" in cancelled.headers["location"]
    assert "synthetic-secret" not in str(cancelled.headers)
    assert oauth_wire.calls == []


@pytest.mark.parametrize(
    "raw",
    [b'{"scope":NaN}', b'{"scope":Infinity}', b'{"client_id":"a","client_id":"b"}'],
)
def test_strict_oauth_json_rejects_ambiguous_values(raw):
    from app.integrations.tiktok.mcp_auth.transport import strict_json

    with pytest.raises(ValueError):
        strict_json(raw)


def test_complete_token_survives_transport_cleanup_failure(
    committed_context, monkeypatch
):
    import httpx2

    from app.integrations.tiktok.mcp_auth import transport

    class CleanupFailure(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, _request):
            return httpx2.Response(
                200,
                json={
                    "access_token": "synthetic-access-secret",
                    "token_type": "Bearer",
                },
            )

        async def aclose(self):
            raise RuntimeError("synthetic-secret-cleanup")

    monkeypatch.setattr(transport, "_new_oauth_transport", CleanupFailure)
    state, identity = issue(committed_context)
    assert accept(state) == identity


def test_protocol_unverified_distinct_from_registration():
    from dataclasses import replace

    from app.integrations.tiktok.mcp_auth.service import load_registration

    # 固定官方 issuer/resource 保持原样，仅把缺失证明的部署状态显式置为未核实。
    unverified = replace(load_mcp_protocol(), authorization_evidence="UNVERIFIED")
    with pytest.raises(DomainError) as error:
        load_registration(unverified)
    assert error.value.code == "mcp_protocol_unverified"


def test_registration_material_rejects_uri_mismatch_and_extra_secret(
    session, monkeypatch, tmp_path
):
    context = create_context(session, role="tenant_admin")
    path = tmp_path / "bad-registration.json"
    profile = load_mcp_protocol()
    path.write_text(
        json.dumps(
            {
                "client_id": "synthetic",
                "issuer": profile.issuer,
                "resource": profile.resource,
                "token_endpoint_auth_method": "none",
                "redirect_uris": ["https://evil.invalid/callback"],
                "client_secret": "synthetic-secret",
            }
        )
    )
    monkeypatch.setattr(settings, "MCP_CLIENT_REGISTRATION_REF", str(path))
    with pytest.raises(DomainError) as error:
        start_mcp_authorization(session, context=context, connection_id=None)
    assert error.value.code == "mcp_client_registration_invalid"
    assert error.value.__context__ is None


def test_oauth_http_redirect_is_not_followed(committed_context, oauth_wire):
    state, _ = issue(committed_context)
    oauth_wire.status = 307
    oauth_wire.headers = {
        "content-type": "application/json",
        "location": "https://evil.invalid?secret=synthetic",
    }
    with pytest.raises(DomainError):
        accept(state)
    assert len(oauth_wire.calls) == 1


def test_operator_http_403_without_oauth_request(client, committed_context, oauth_wire):
    from datetime import timedelta

    from sqlmodel import Session

    from app.core.db import engine
    from app.core.security import create_access_token
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as own:
        member = own.get(
            TenantMembership, (committed_context.tenant_id, committed_context.actor_id)
        )
        member.role = "operator"
        own.add(member)
        own.commit()
    token = create_access_token(committed_context.actor_id, timedelta(minutes=5))
    response = client.post(
        f"/api/tenants/{committed_context.tenant_id}/tiktok/mcp/authorizations",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "action_forbidden"
    assert oauth_wire.calls == []


def test_concurrent_callback_consumes_code_once(committed_context, oauth_wire):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, finish = Event(), Event()
    state, identity = issue(committed_context)

    def before():
        entered.set()
        assert finish.wait(5)

    oauth_wire.before = before
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(accept, state)
        assert entered.wait(5)
        second = executor.submit(accept, state)
        try:
            with pytest.raises(DomainError) as error:
                second.result(timeout=5)
            assert error.value.code == "invalid_oauth_state"
        finally:
            finish.set()
        assert first.result(timeout=5) == identity
    assert len(oauth_wire.calls) == 1


def test_oauth_timeout_is_bounded_without_live_request(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from time import monotonic

    import anyio
    import httpx2

    from app.integrations.tiktok.mcp_auth import transport

    async def delayed(_request):
        await anyio.sleep(5)
        return httpx2.Response(
            200, json={"access_token": "synthetic", "token_type": "Bearer"}
        )

    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(delayed)
    )
    started = monotonic()
    with pytest.raises(DomainError) as error:
        transport.exchange_token(
            endpoint=load_mcp_protocol().token_endpoint,
            form={"code": "synthetic-private-code"},
            task_deadline=datetime.now(UTC) + timedelta(milliseconds=100),
        )
    assert error.value.code == "mcp_oauth_result_unknown"
    assert error.value.__context__ is None
    assert monotonic() - started < 1


def test_global_admin_without_tenant_membership_cannot_authorize(session):
    from uuid import uuid4

    from app.core.context import TenantContext
    from app.models import User
    from app.modules.accounts.models import TikTokConnection
    from app.modules.tenants.models import Tenant

    actor = User(username=str(uuid4()), hashed_password="unused", is_superuser=True)
    tenant = Tenant(name=f"synthetic-{uuid4()}")
    session.add_all([actor, tenant])
    session.flush()
    context = TenantContext(
        tenant_id=tenant.id, actor_id=actor.id, role="platform_admin"
    )
    with pytest.raises(DomainError) as error:
        start_mcp_authorization(session, context=context, connection_id=None)
    assert error.value.code == "action_forbidden"
    assert (
        session.exec(
            select(TikTokConnection).where(TikTokConnection.tenant_id == tenant.id)
        ).all()
        == []
    )
    assert (
        session.exec(
            select(McpAuthorizationAttempt).where(
                McpAuthorizationAttempt.tenant_id == tenant.id
            )
        ).all()
        == []
    )


@pytest.mark.parametrize("cleanup", ["response", "client"])
def test_receipt_callback_precedes_oauth_cleanup(monkeypatch, cleanup):
    from datetime import UTC, datetime, timedelta

    import httpx2

    from app.integrations.tiktok.mcp_auth import transport

    events = []

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"access_token":"synthetic-access-secret","token_type":"Bearer"}'

        async def aclose(self):
            events.append("response_cleanup")
            if cleanup == "response":
                raise RuntimeError("synthetic-response-cleanup-failure")

    class Wire(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, _request):
            return httpx2.Response(
                200, headers={"content-type": "application/json"}, stream=Body()
            )

        async def aclose(self):
            events.append("client_cleanup")
            if cleanup == "client":
                raise RuntimeError("synthetic-client-cleanup-failure")

    def persist(value):
        assert value["access_token"] == "synthetic-access-secret"
        events.append("persisted")

    monkeypatch.setattr(transport, "_new_oauth_transport", Wire)
    result = transport.exchange_token(
        endpoint=load_mcp_protocol().token_endpoint,
        form={"code": "synthetic"},
        task_deadline=datetime.now(UTC) + timedelta(seconds=5),
        on_received=persist,
    )
    assert result["access_token"] == "synthetic-access-secret"
    assert events == ["persisted", "response_cleanup", "client_cleanup"]


def test_receipt_callback_failure_never_returns_oauth_success(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import httpx2

    from app.integrations.tiktok.mcp_auth import transport

    events = []

    async def request(_request):
        events.append("request")
        return httpx2.Response(
            200,
            json={"access_token": "synthetic-access-secret", "token_type": "Bearer"},
        )

    def reject(_value):
        events.append("persistence_failed")
        raise RuntimeError("synthetic-secret-persistence-failure")

    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(request)
    )
    with pytest.raises(DomainError) as error:
        transport.exchange_token(
            endpoint=load_mcp_protocol().token_endpoint,
            form={"code": "synthetic"},
            task_deadline=datetime.now(UTC) + timedelta(seconds=5),
            on_received=reject,
        )
    assert error.value.code == "mcp_oauth_result_unknown"
    assert error.value.__context__ is None
    assert "synthetic-secret" not in str(error.value)
    assert events == ["request", "persistence_failed"]


@pytest.mark.parametrize("phase", ["redeem", "publish", "cancel"])
def test_superuser_inactive_membership_blocks_callback(
    client, committed_context, oauth_wire, phase
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.models import User
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as own:
        user = own.get(User, committed_context.actor_id)
        user.is_superuser = True
        own.add(user)
        own.commit()
    state, identity = issue(committed_context)

    def revoke_membership():
        with Session(engine) as own:
            member = own.get(
                TenantMembership,
                (committed_context.tenant_id, committed_context.actor_id),
            )
            member.active = False
            own.add(member)
            own.commit()

    if phase == "publish":
        oauth_wire.before = revoke_membership
    else:
        revoke_membership()
    params = {"state": state, "code": "synthetic-private-code"}
    if phase == "cancel":
        params = {"state": state, "error": "access_denied"}
    response = client.get(
        "/api/integrations/tiktok/mcp/callback", params=params, follow_redirects=False
    )
    assert response.status_code == 303
    assert "action_forbidden" in response.headers["location"]
    assert len(oauth_wire.calls) == (1 if phase == "publish" else 0)
    with Session(engine) as own:
        attempt = own.get(McpAuthorizationAttempt, identity)
        assert attempt.candidate_ciphertext is None
        assert attempt.status == ("FAILED" if phase == "publish" else "PENDING")
