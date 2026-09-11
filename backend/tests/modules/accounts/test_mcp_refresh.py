"""Synthetic HTTP responses with committed PostgreSQL rows and real Redis."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
from sqlmodel import Session, col, delete, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth import transport
from app.integrations.tiktok.mcp_auth.refresh import (
    ensure_mcp_credentials,
    process_mcp_refresh,
)
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.models import User
from app.modules.accounts.connection_models import (
    ConnectionAuthorization,
    McpRefreshAttempt,
)
from app.modules.accounts.models import TikTokConnection
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.conftest import create_context


@pytest.fixture
def database_engine():
    from app.core.db import engine
    from tests.database import require_test_database

    require_test_database(str(settings.DATABASE_URL))
    assert engine.dialect.name == "postgresql"
    return engine


@pytest.fixture
def refresh_config(monkeypatch, tmp_path, policy, redis_client):
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
    monkeypatch.setattr(settings, "MCP_SERVICE_QUOTA_SCOPE", f"test-refresh-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    yield
    # Only remove this fixture's unique quota scope keys.
    from hashlib import sha256

    digest = sha256(
        f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}".encode()
    ).hexdigest()
    keys = list(redis_client.scan_iter(f"tiktok:{{{digest}}}:*"))
    if keys:
        redis_client.delete(*keys)


@pytest.fixture
def mcp_refresh_case(database_engine, refresh_config):
    assert refresh_config is None
    profile = load_mcp_protocol()
    with Session(database_engine) as session:
        context = create_context(session, role="tenant_admin")
        connection = TikTokConnection(
            tenant_id=context.tenant_id,
            kind="OFFICIAL_MCP",
            status="ACTIVE",
            credential_revision=1,
            authorization_revision=1,
            service_profile=profile.revision,
            credential_ciphertext=encrypt_credentials(
                tenant_id=context.tenant_id,
                value={
                    "access_token": "synthetic-old-access",
                    "refresh_token": "synthetic-old-refresh",
                    "client_id": "synthetic-client",
                    "token_type": "Bearer",
                    "scopes": '["mcp:tt4b"]',
                    "issuer": profile.issuer,
                    "resource": profile.resource,
                    "received_at": datetime.now(UTC).isoformat(),
                    "expires_at": (
                        datetime.now(UTC) + timedelta(seconds=20)
                    ).isoformat(),
                },
            ),
        )
        session.add(connection)
        session.flush()
        session.add(
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=1,
                issuer=profile.issuer,
                resource=profile.resource,
                scopes=["mcp:tt4b"],
                source="OAUTH_TOKEN_RESPONSE",
                permission_summary={},
            )
        )
        attempt = McpRefreshAttempt(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            base_credential_revision=1,
            base_authorization_revision=1,
        )
        session.add(attempt)
        session.commit()
        result = context, connection.id, attempt.id
    try:
        yield result
    finally:
        with Session(database_engine) as session:
            for model in (
                PendingDispatch,
                DispatchTenantCursor,
                McpRefreshAttempt,
                ConnectionAuthorization,
                TikTokConnection,
                TenantMembership,
            ):
                session.exec(delete(model).where(model.tenant_id == context.tenant_id))
            session.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
            session.exec(delete(User).where(User.id == context.actor_id))
            session.commit()


@pytest.fixture
def refresh_wire(monkeypatch):
    class Wire:
        def __init__(self):
            self.calls = []
            self.before = None
            self.fail = False
            self.data = {
                "access_token": "synthetic-new-access",
                "refresh_token": "synthetic-new-refresh",
                "token_type": "Bearer",
                "scope": "mcp:tt4b",
                "expires_in": 3600,
            }

        async def request(self, request):
            assert request.method == "POST"
            assert str(request.url) == load_mcp_protocol().token_endpoint
            self.calls.append(request)
            if self.before:
                self.before()
            if self.fail:
                raise httpx2.ReadError("synthetic response lost")
            return httpx2.Response(200, json=self.data)

    wire = Wire()
    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(wire.request)
    )
    return wire


def test_unknown_rotation_does_not_consume_old_refresh_again(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, attempt_id = mcp_refresh_case
    refresh_wire.fail = True
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "OUTCOME_UNKNOWN"
    )
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "OUTCOME_UNKNOWN"
    )
    assert len(refresh_wire.calls) == 1
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        assert material["access_token"] == "synthetic-old-access"
        assert connection.credential_revision == 1
    with pytest.raises(DomainError) as error:
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
    assert error.value.code == "mcp_refresh_unknown"
    assert len(refresh_wire.calls) == 1


def test_insufficient_expiry_persists_work_without_http(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, attempt_id = mcp_refresh_case
    with pytest.raises(DomainError) as error:
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    assert error.value.code == "mcp_refresh_pending"
    assert refresh_wire.calls == []
    with Session(database_engine) as session:
        assert session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        ).one().payload == {"attempt_id": str(attempt_id)}


def test_rotation_keeps_authorization_revision(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, attempt_id = mcp_refresh_case
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert connection.authorization_revision == 1
        assert connection.credential_revision == 2
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        assert facts.upstream_grant_id is None
        assert facts.upstream_subject is None
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        assert material["access_token"] == "synthetic-new-access"
        assert material["refresh_token"] == "synthetic-new-refresh"
    ensure_mcp_credentials(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", ""),
        ("scope", "mcp:tt4b new:scope"),
        ("subject", "new-subject"),
        ("grant_id", "new-grant"),
        ("bc_id", "new-bc"),
        ("issuer", "https://invalid.test"),
        ("resource", "https://invalid.test"),
        ("permission_summary", {"build": False}),
    ],
)
def test_changed_authority_retains_candidate_and_fences_operations(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, field, value
):
    context, connection_id, attempt_id = mcp_refresh_case
    refresh_wire.data[field] = value
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "CANDIDATE_READY"
    )
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        attempt = session.get(McpRefreshAttempt, attempt_id)
        assert connection.status == "REAUTH_REQUIRED"
        assert connection.authorization_revision == 2
        assert connection.credential_revision == 1
        receipt = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=attempt.candidate_ciphertext
        )
        assert json.loads(receipt["response"])[field] == value
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "CANDIDATE_READY"
    )
    assert len(refresh_wire.calls) == 1
    with pytest.raises(DomainError) as error:
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
    assert error.value.code == "mcp_refresh_reauth_required"


@pytest.mark.parametrize("mutation", ["DISABLED", "REAUTHORIZED"])
def test_late_response_cannot_overwrite_new_authority(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, mutation
):
    context, connection_id, attempt_id = mcp_refresh_case

    def change():
        with Session(database_engine) as session:
            connection = session.get(TikTokConnection, connection_id)
            connection.authorization_revision += 1
            if mutation == "DISABLED":
                connection.status = "DISABLED"
            else:
                connection.credential_revision += 1
                connection.credential_ciphertext = encrypt_credentials(
                    tenant_id=context.tenant_id,
                    value={"access_token": "synthetic-reauthorized"},
                )
            session.add(connection)
            session.commit()

    refresh_wire.before = change
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "SUPERSEDED"
    )
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert connection.authorization_revision == 2
        assert connection.status == ("DISABLED" if mutation == "DISABLED" else "ACTIVE")
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        assert material["access_token"] == (
            "synthetic-old-access"
            if mutation == "DISABLED"
            else "synthetic-reauthorized"
        )
        assert session.get(McpRefreshAttempt, attempt_id).candidate_ciphertext
    assert len(refresh_wire.calls) == 1


def test_absent_scope_refresh_inherits_original_scope(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    _, _, attempt_id = mcp_refresh_case
    refresh_wire.data.pop("scope")
    refresh_wire.data.pop("refresh_token")
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )


def test_stale_armed_attempt_never_replays(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    _, _, attempt_id = mcp_refresh_case
    with Session(database_engine) as session:
        attempt = session.get(McpRefreshAttempt, attempt_id)
        attempt.status = "REQUEST_ARMED"
        attempt.claim_id = uuid4()
        attempt.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        attempt.request_armed_at = datetime.now(UTC) - timedelta(seconds=50)
        session.add(attempt)
        session.commit()
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "OUTCOME_UNKNOWN"
    )
    assert refresh_wire.calls == []


def test_complete_candidate_survives_cleanup_interruption(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, monkeypatch
):
    context, connection_id, attempt_id = mcp_refresh_case

    class InterruptedStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(refresh_wire.data).encode()

        async def aclose(self):
            # HTTP response cleanup sees the already committed complete candidate.
            with Session(database_engine) as session:
                attempt = session.get(McpRefreshAttempt, attempt_id)
                assert attempt.status == "CANDIDATE_READY"
                assert attempt.candidate_ciphertext
            raise KeyboardInterrupt("synthetic process interrupted after receipt")

    async def wire(request):
        refresh_wire.calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=InterruptedStream(),
        )

    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(wire)
    )
    with pytest.raises(KeyboardInterrupt):
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1


def test_sufficient_expiry_does_not_refresh(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, _ = mcp_refresh_case
    ensure_mcp_credentials(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        task_deadline=datetime.now(UTC) + timedelta(seconds=5),
    )
    assert refresh_wire.calls == []


def test_client_registration_change_prevents_consuming_refresh(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    _, _, attempt_id = mcp_refresh_case
    from pathlib import Path

    path = Path(settings.MCP_CLIENT_REGISTRATION_REF)
    registration = json.loads(path.read_text())
    registration["client_id"] = "another-client"
    path.write_text(json.dumps(registration))
    with pytest.raises(DomainError) as error:
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
    assert error.value.code == "mcp_refresh_reauth_required"
    assert refresh_wire.calls == []


def test_new_authorization_candidate_fences_late_refresh(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    from app.modules.accounts.connection_models import McpAuthorizationAttempt

    context, connection_id, attempt_id = mcp_refresh_case
    profile = load_mcp_protocol()
    candidate_id = uuid4()

    def authorize():
        with Session(database_engine) as session:
            session.add(
                McpAuthorizationAttempt(
                    id=candidate_id,
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    connection_id=connection_id,
                    base_credential_revision=1,
                    base_authorization_revision=1,
                    issuer=profile.issuer,
                    resource=profile.resource,
                    redirect_uri=settings.MCP_REDIRECT_URI,
                    state_hash=uuid4().hex,
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            session.commit()

    refresh_wire.before = authorize
    try:
        assert (
            process_mcp_refresh(
                context=mcp_refresh_case[0],
                database_engine=database_engine,
                redis_client=redis_client,
                attempt_id=attempt_id,
            )
            == "SUPERSEDED"
        )
        with Session(database_engine) as session:
            assert session.get(TikTokConnection, connection_id).credential_revision == 1
            assert session.get(McpRefreshAttempt, attempt_id).candidate_ciphertext
    finally:
        with Session(database_engine) as session:
            session.exec(
                delete(McpAuthorizationAttempt).where(
                    McpAuthorizationAttempt.id == candidate_id
                )
            )
            session.commit()


def test_worker_recovery_is_durable_before_network(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, monkeypatch
):
    from app.modules.accounts.refresh_tasks import refresh_mcp

    context, _, attempt_id = mcp_refresh_case
    # Use the real test Redis selected by the guarded fixture, not production settings.
    import os

    assert redis_client.ping()
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])

    def check():
        with Session(database_engine) as session:
            recovery = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id
                )
            ).one()
            assert recovery.available_at > datetime.now(UTC)
            assert recovery.payload == {"attempt_id": str(attempt_id)}

    refresh_wire.before = check
    assert (
        refresh_mcp(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload={"attempt_id": str(attempt_id)},
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1


def test_sent_superseded_attempt_cannot_be_requeued_with_old_token(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, attempt_id = mcp_refresh_case
    with Session(database_engine) as session:
        attempt = session.get(McpRefreshAttempt, attempt_id)
        attempt.status = "SUPERSEDED"
        attempt.request_armed_at = datetime.now(UTC)
        session.add(attempt)
        # A distinct attempt must not bypass the earlier sent-token evidence.
        successor = McpRefreshAttempt(
            tenant_id=context.tenant_id,
            connection_id=connection_id,
            base_credential_revision=1,
            base_authorization_revision=1,
        )
        session.add(successor)
        session.commit()
        successor_id = successor.id
    with pytest.raises(DomainError) as error:
        ensure_mcp_credentials(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            task_deadline=datetime.now(UTC) + timedelta(seconds=5),
        )
    assert error.value.code == "mcp_refresh_unknown"
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=successor_id,
        )
        == "SUPERSEDED"
    )
    assert refresh_wire.calls == []


def test_foreign_connection_receipt_cannot_publish(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, monkeypatch
):
    context, connection_id, attempt_id = mcp_refresh_case

    class InterruptedStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(refresh_wire.data).encode()

        async def aclose(self):
            with Session(database_engine) as session:
                attempt = session.get(McpRefreshAttempt, attempt_id)
                receipt = decrypt_credentials(
                    tenant_id=context.tenant_id, ciphertext=attempt.candidate_ciphertext
                )
                receipt["connection_id"] = str(uuid4())
                attempt.candidate_ciphertext = encrypt_credentials(
                    tenant_id=context.tenant_id, value=receipt
                )
                session.add(attempt)
                session.commit()
            raise KeyboardInterrupt("synthetic receipt recovery")

    async def wire(request):
        refresh_wire.calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            stream=InterruptedStream(),
        )

    monkeypatch.setattr(
        transport, "_new_oauth_transport", lambda: httpx2.MockTransport(wire)
    )
    with pytest.raises(KeyboardInterrupt):
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "CANDIDATE_READY"
    )
    with Session(database_engine) as session:
        assert session.get(TikTokConnection, connection_id).credential_revision == 1
    assert len(refresh_wire.calls) == 1


def test_real_redis_denial_keeps_request_unarmed(
    database_engine, redis_client, mcp_refresh_case, refresh_wire, policy
):
    from app.integrations.tiktok.admission import quota_scope
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope

    context, _, attempt_id = mcp_refresh_case
    policy.app_max_inflight = 1
    settings.TIKTOK_CALL_POLICIES = {"base": policy.model_dump()}
    scope = quota_scope(
        channel="OFFICIAL_MCP",
        app_id=None,
        verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
    )
    with admitted_scope(
        redis_client,
        app_scope=scope,
        endpoint="auth_refresh",
        tenant_id=context.tenant_id,
        advertiser_id="",
        policy=policy,
        denied_error=AccountAdmissionDeferred,
    ):
        assert (
            process_mcp_refresh(
                context=mcp_refresh_case[0],
                database_engine=database_engine,
                redis_client=redis_client,
                attempt_id=attempt_id,
            )
            == "PENDING"
        )
    with Session(database_engine) as session:
        attempt = session.get(McpRefreshAttempt, attempt_id)
        assert attempt.request_armed_at is None
        assert attempt.claim_id is None
    assert refresh_wire.calls == []


def test_unknown_new_token_expiry_requires_reauthorization(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    _, connection_id, attempt_id = mcp_refresh_case
    refresh_wire.data.pop("expires_in")
    assert (
        process_mcp_refresh(
            context=mcp_refresh_case[0],
            database_engine=database_engine,
            redis_client=redis_client,
            attempt_id=attempt_id,
        )
        == "CANDIDATE_READY"
    )
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert connection.credential_revision == 1
        assert connection.status == "REAUTH_REQUIRED"
    assert len(refresh_wire.calls) == 1


def test_published_dispatch_can_be_repaired_by_same_actor(
    database_engine, redis_client, mcp_refresh_case, refresh_wire
):
    context, connection_id, attempt_id = mcp_refresh_case
    for round_no in range(2):
        with pytest.raises(DomainError) as error:
            ensure_mcp_credentials(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                connection_id=connection_id,
                task_deadline=datetime.now(UTC) + timedelta(seconds=30),
            )
        assert error.value.code == "mcp_refresh_pending"
        with Session(database_engine) as session:
            rows = session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == context.tenant_id
                )
            ).all()
            assert len(rows) == round_no + 1
            pending = [row for row in rows if row.published_at is None]
            assert len(pending) == 1
            assert pending[0].payload == {"attempt_id": str(attempt_id)}
            pending[0].published_at = datetime.now(UTC)
            session.add(pending[0])
            session.commit()
        if round_no == 0:
            from app.modules.accounts.refresh_tasks import refresh_mcp

            with Session(database_engine) as session:
                member = session.get(
                    TenantMembership, (context.tenant_id, context.actor_id)
                )
                member.active = False
                session.add(member)
                session.commit()
            with pytest.raises(DomainError) as failed:
                refresh_mcp(
                    tenant_id=str(context.tenant_id),
                    actor_id=str(context.actor_id),
                    payload={"attempt_id": str(attempt_id)},
                )
            assert failed.value.code == "tenant_forbidden"
            with Session(database_engine) as session:
                member = session.get(
                    TenantMembership, (context.tenant_id, context.actor_id)
                )
                member.active = True
                session.add(member)
                session.commit()
    assert refresh_wire.calls == []


@pytest.fixture
def second_refresh_actor(database_engine, mcp_refresh_case):
    from app.core.context import TenantContext

    original, _, _ = mcp_refresh_case
    with Session(database_engine) as session:
        user = User(username=str(uuid4()), hashed_password="unused")
        session.add(user)
        session.flush()
        context = TenantContext(
            tenant_id=original.tenant_id, actor_id=user.id, role="operator"
        )
        session.add(
            TenantMembership(
                tenant_id=context.tenant_id, user_id=context.actor_id, role="operator"
            )
        )
        session.commit()
    try:
        yield context
    finally:
        with Session(database_engine) as session:
            session.exec(
                delete(TenantMembership).where(
                    TenantMembership.user_id == context.actor_id
                )
            )
            session.exec(delete(User).where(User.id == context.actor_id))
            session.commit()


def test_new_actor_recovers_received_candidate_after_original_actor_revoked(
    database_engine,
    redis_client,
    mcp_refresh_case,
    refresh_wire,
    second_refresh_actor,
    monkeypatch,
):
    import os

    from app.modules.accounts.refresh_tasks import refresh_mcp

    original, connection_id, attempt_id = mcp_refresh_case
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])

    def revoke_after_send():
        with Session(database_engine) as session:
            member = session.get(
                TenantMembership, (original.tenant_id, original.actor_id)
            )
            member.active = False
            session.add(member)
            session.commit()

    refresh_wire.before = revoke_after_send
    with pytest.raises(DomainError) as error:
        refresh_mcp(
            tenant_id=str(original.tenant_id),
            actor_id=str(original.actor_id),
            payload={"attempt_id": str(attempt_id)},
        )
    assert error.value.code == "tenant_forbidden"
    with Session(database_engine) as session:
        attempt = session.get(McpRefreshAttempt, attempt_id)
        assert attempt.status == "CANDIDATE_READY" and attempt.candidate_ciphertext
        assert session.get(TikTokConnection, connection_id).credential_revision == 1
        # The old identity's recovery delivery is consumed by its failing preflight.
        for dispatch in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == original.tenant_id
            )
        ).all():
            dispatch.published_at = datetime.now(UTC)
            session.add(dispatch)
        session.commit()
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            ensure_mcp_credentials(
                database_engine=database_engine,
                redis_client=redis_client,
                context=second_refresh_actor,
                connection_id=connection_id,
                task_deadline=datetime.now(UTC) + timedelta(seconds=5),
            )
        assert error.value.code == "mcp_refresh_pending"
    with Session(database_engine) as session:
        pending = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == original.tenant_id,
                PendingDispatch.actor_id == second_refresh_actor.actor_id,
                col(PendingDispatch.published_at).is_(None),
            )
        ).all()
        assert len(pending) == 1
        pending[0].published_at = datetime.now(UTC)
        session.add(pending[0])
        session.commit()
    assert (
        refresh_mcp(
            tenant_id=str(second_refresh_actor.tenant_id),
            actor_id=str(second_refresh_actor.actor_id),
            payload={"attempt_id": str(attempt_id)},
        )
        == "PUBLISHED"
    )
    assert (
        process_mcp_refresh(
            database_engine=database_engine,
            redis_client=redis_client,
            context=second_refresh_actor,
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(refresh_wire.calls) == 1
    with Session(database_engine) as session:
        assert session.get(TikTokConnection, connection_id).credential_revision == 2
