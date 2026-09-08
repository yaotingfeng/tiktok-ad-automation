import socket
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.jobs.admission import AdmissionPolicy
from app.modules.accounts.models import AuthorizationAttempt, TikTokConnection
from app.modules.tenants.models import TenantMembership


@pytest.fixture(autouse=True)
def app_config(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"test-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "fake-app-secret")
    monkeypatch.setattr(
        settings,
        "TIKTOK_REDIRECT_URI",
        "https://app.example.com/api/integrations/tiktok/callback",
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_AUTHORIZATION_URL",
        "https://business-api.tiktok.com/portal/auth?app_id=original&custom=keep&state=discard",
    )
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
def policy():
    return AdmissionPolicy(
        app_max_inflight=10,
        endpoint_max_inflight=10,
        tenant_max_inflight=10,
        advertiser_max_inflight=10,
        app_calls_per_window=100,
        endpoint_calls_per_window=100,
        window_ms=1000,
        lease_ms=60000,
    )


@pytest.fixture
def auth_attempt(session, context):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    connection = TikTokConnection(tenant_id=context.tenant_id, status="PENDING_AUTH")
    session.add(connection)
    session.flush()
    attempt = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        state_hash=sha256(b"fake-state").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        status="PENDING",
    )
    session.add(attempt)
    session.flush()
    return attempt


@pytest.fixture
def sdk_transport(monkeypatch):
    """Keep generated SDK serialization/deserialization; fake only urllib3 request."""
    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(
            body=b'{"code":0,"data":{"access_token":"fake-token"}}',
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    from app.integrations.tiktok import auth

    def exchange(*, deadline_seconds, **kwargs):
        assert deadline_seconds > 0
        return auth._token_request(**kwargs)

    monkeypatch.setattr(auth, "_exchange_token", exchange)
    # Real local Postgres/Redis remain usable; reject outbound TikTok sockets.
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host not in {"localhost", "127.0.0.1", "::1", None}:
            pytest.fail("External networking is forbidden in accounts tests")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return calls
