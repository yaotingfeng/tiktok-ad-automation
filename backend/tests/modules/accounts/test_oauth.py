from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.auth import (
    claim_authorization,
    finish_authorization,
    start_authorization,
)
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.accounts.models import AuthorizationAttempt, TikTokConnection
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.conftest import create_context


def test_state_is_claimed_once(session, auth_attempt):
    first = claim_authorization(session, state="fake-state", now=datetime.now(UTC))
    assert first.id == auth_attempt.id
    with pytest.raises(DomainError) as error:
        claim_authorization(session, state="fake-state", now=datetime.now(UTC))
    assert error.value.code == "invalid_oauth_state"


@pytest.mark.parametrize(
    "change", ["expired", "wrong", "disabled_actor", "revoked_role"]
)
def test_state_checks_and_live_permission(session, auth_attempt, change):
    if change == "expired":
        auth_attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if change == "disabled_actor":
        session.get(User, auth_attempt.actor_id).is_active = False
    if change == "revoked_role":
        session.get(
            TenantMembership, (auth_attempt.tenant_id, auth_attempt.actor_id)
        ).role = "viewer"
    session.flush()
    with pytest.raises(DomainError):
        claim_authorization(
            session,
            state="wrong" if change == "wrong" else "fake-state",
            now=datetime.now(UTC),
        )


def test_start_binds_context_stores_digest_and_preserves_old_credentials(
    session, context, auth_attempt
):
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    connection.credential_ciphertext = encrypt_credentials(
        tenant_id=context.tenant_id, value={"access_token": "old"}
    )
    connection.credential_version = 4
    session.flush()
    url = start_authorization(session, context=context, connection_id=connection.id)
    query = parse_qs(urlsplit(url).query)
    state = query["state"][0]
    assert len(state) >= 40
    assert query["custom"] == ["keep"]
    assert query["app_id"] == [settings.TIKTOK_APP_ID]
    attempt = session.exec(
        select(AuthorizationAttempt).where(
            AuthorizationAttempt.state_hash == sha256(state.encode()).hexdigest()
        )
    ).one()
    assert (attempt.tenant_id, attempt.actor_id, attempt.connection_id) == (
        context.tenant_id,
        context.actor_id,
        connection.id,
    )
    assert attempt.base_credential_version == 4
    assert state not in repr(attempt)
    assert connection.status == "ACTIVE" and connection.credential_version == 4


@pytest.mark.parametrize(
    "url",
    [
        "http://ads.tiktok.com/auth",
        "https://ads.tiktok.com.evil.test/auth",
        "https://fake@ads.tiktok.com/auth",
        "https://ads.tiktok.com:444/auth",
        "https://www.tiktok.com/v2/auth/authorize/",
    ],
)
def test_authorization_url_is_official_https_only(
    session, context, auth_attempt, monkeypatch, url
):
    monkeypatch.setattr(settings, "TIKTOK_AUTHORIZATION_URL", url)
    with pytest.raises(DomainError) as error:
        start_authorization(
            session, context=context, connection_id=auth_attempt.connection_id
        )
    assert error.value.code == "invalid_authorization_url"


@pytest.mark.usefixtures("auth_attempt")
def test_start_cross_tenant_connection_is_hidden(session, context, other_context):
    foreign = TikTokConnection(tenant_id=other_context.tenant_id)
    session.add(foreign)
    session.flush()
    with pytest.raises(DomainError) as error:
        start_authorization(session, context=context, connection_id=foreign.id)
    assert error.value.code == "connection_not_found"


def test_finish_stages_candidate_and_transactional_dispatch(
    session, auth_attempt, sdk_transport, redis_client, policy
):
    connection_id = finish_authorization(
        session,
        state="fake-state",
        auth_code="fake-code",
        now=datetime.now(UTC),
        redis_client=redis_client,
        policy=policy,
    )
    connection = session.get(TikTokConnection, connection_id)
    session.refresh(auth_attempt)
    assert connection.status == "DISCOVERING"
    assert connection.credential_ciphertext is None
    assert auth_attempt.status == "CANDIDATE_READY"
    assert decrypt_credentials(
        tenant_id=auth_attempt.tenant_id, ciphertext=auth_attempt.candidate_ciphertext
    ) == {"access_token": "fake-token"}
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == auth_attempt.tenant_id
        )
    ).one()
    assert dispatch.payload == {"attempt_id": str(auth_attempt.id)}
    assert dispatch.task_name == "accounts.discover"
    assert "fake-token" not in repr(dispatch)
    assert len(sdk_transport) == 1
    method, url, request = sdk_transport[0]
    assert method == "POST" and url.endswith("/open_api/v1.3/oauth2/access_token/")
    assert '"auth_code": "fake-code"' in request["body"]
    assert '"secret": "fake-app-secret"' in request["body"]
    with pytest.raises(DomainError) as error:
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=redis_client,
            policy=policy,
        )
    assert error.value.code == "invalid_oauth_state"
    assert len(sdk_transport) == 1


def test_two_postgres_transactions_only_one_can_claim():
    with Session(engine) as setup:
        context = create_context(setup, role="tenant_admin")
        connection = TikTokConnection(tenant_id=context.tenant_id)
        setup.add(connection)
        setup.flush()
        state = f"concurrent-{uuid4()}"
        attempt = AuthorizationAttempt(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=connection.id,
            state_hash=sha256(state.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        setup.add(attempt)
        setup.commit()
    barrier = Barrier(2)

    def claim(_):
        with Session(engine) as transaction:
            barrier.wait(timeout=5)
            try:
                claim_authorization(transaction, state=state, now=datetime.now(UTC))
                transaction.commit()
                return "claimed"
            except DomainError as error:
                transaction.rollback()
                return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, range(2))) == [
                "claimed",
                "invalid_oauth_state",
            ]
    finally:
        with Session(engine) as cleanup:
            for model in [
                AuthorizationAttempt,
                TikTokConnection,
                TenantMembership,
                Tenant,
            ]:
                field = model.id if model is Tenant else model.tenant_id
                cleanup.execute(delete(model).where(field == context.tenant_id))
            cleanup.execute(delete(User).where(User.id == context.actor_id))
            cleanup.commit()


def test_admission_denial_preserves_pending_state(
    session, auth_attempt, monkeypatch, policy
):
    from unittest.mock import Mock

    from app.integrations.tiktok import auth, sdk
    from app.jobs.admission import Admission

    remote = Mock()
    monkeypatch.setattr(auth, "_exchange_token", remote)
    monkeypatch.setattr(sdk, "admit_call", Mock(return_value=Admission(False, 800)))
    with pytest.raises(sdk.AccountAdmissionDeferred):
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=Mock(),
            policy=policy,
        )
    session.refresh(auth_attempt)
    assert auth_attempt.status == "PENDING" and auth_attempt.claimed_at is None
    remote.assert_not_called()


@pytest.mark.parametrize(
    "code,status",
    [
        ("oauth_result_unknown", "RESULT_UNKNOWN"),
        ("tiktok_response_error", "FAILED"),
        ("invalid_token_response", "FAILED"),
    ],
)
def test_exchange_failure_preserves_old_active_version(
    session, auth_attempt, monkeypatch, redis_client, policy, code, status
):
    from app.integrations.tiktok import auth

    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    encrypted = encrypt_credentials(
        tenant_id=auth_attempt.tenant_id, value={"access_token": "old-token"}
    )
    connection.credential_ciphertext = encrypted
    connection.credential_version = 8
    session.flush()

    def fail(**_kwargs):
        raise DomainError(code, "fake-token")

    monkeypatch.setattr(auth, "_exchange_token", fail)
    with pytest.raises(DomainError) as error:
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=redis_client,
            policy=policy,
        )
    assert error.value.code == code and "fake-token" not in str(error.value)
    session.refresh(auth_attempt)
    session.refresh(connection)
    assert (
        connection.status,
        connection.credential_version,
        connection.credential_ciphertext,
    ) == ("ACTIVE", 8, encrypted)
    assert auth_attempt.status == status and auth_attempt.candidate_ciphertext is None
    assert (
        session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == auth_attempt.tenant_id
            )
        ).all()
        == []
    )


def test_reauthorization_success_keeps_active_credentials_until_discovery(
    session, auth_attempt, sdk_transport, redis_client, policy
):
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    encrypted = encrypt_credentials(
        tenant_id=auth_attempt.tenant_id, value={"access_token": "old-token"}
    )
    connection.credential_ciphertext = encrypted
    connection.credential_version = 8
    session.flush()
    finish_authorization(
        session,
        state="fake-state",
        auth_code="fake-code",
        now=datetime.now(UTC),
        redis_client=redis_client,
        policy=policy,
    )
    session.refresh(connection)
    assert len(sdk_transport) == 1
    assert (
        connection.status,
        connection.credential_version,
        connection.credential_ciphertext,
    ) == ("ACTIVE", 8, encrypted)


@pytest.mark.parametrize("revoke", ["actor", "membership", "tenant", "connection"])
def test_permission_is_reloaded_after_exchange(
    session, auth_attempt, monkeypatch, redis_client, policy, revoke
):
    from app.integrations.tiktok import auth

    def exchange(**_kwargs):
        if revoke == "actor":
            session.get(User, auth_attempt.actor_id).is_active = False
        elif revoke == "membership":
            session.get(
                TenantMembership, (auth_attempt.tenant_id, auth_attempt.actor_id)
            ).role = "viewer"
        elif revoke == "tenant":
            session.get(Tenant, auth_attempt.tenant_id).active = False
        else:
            session.get(
                TikTokConnection, auth_attempt.connection_id
            ).status = "DISABLED"
        session.commit()
        return {"access_token": "fake-token"}

    monkeypatch.setattr(auth, "_exchange_token", exchange)
    with pytest.raises(DomainError):
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=redis_client,
            policy=policy,
        )
    session.refresh(auth_attempt)
    assert auth_attempt.status == "FAILED" and auth_attempt.candidate_ciphertext is None
    assert (
        session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == auth_attempt.tenant_id
            )
        ).all()
        == []
    )


def test_outbox_failure_rolls_back_candidate_and_discovering_status(
    session, auth_attempt, sdk_transport, redis_client, policy, monkeypatch
):
    from app.integrations.tiktok import auth

    def fail(*_args, **_kwargs):
        raise DomainError("dispatch_task_not_allowed", "not registered")

    monkeypatch.setattr(auth, "enqueue_after_commit", fail)
    with pytest.raises(DomainError):
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=redis_client,
            policy=policy,
        )
    session.refresh(auth_attempt)
    assert len(sdk_transport) == 1
    assert auth_attempt.status == "FAILED" and auth_attempt.candidate_ciphertext is None
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    assert connection.status == "PENDING_AUTH"
    assert (
        session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == auth_attempt.tenant_id
            )
        ).all()
        == []
    )


def test_cancellation_does_not_touch_active_connection(session, auth_attempt):
    from app.integrations.tiktok.auth import cancel_authorization

    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    session.flush()
    assert (
        cancel_authorization(session, state="fake-state", now=datetime.now(UTC))
        == connection.id
    )
    session.refresh(auth_attempt)
    session.refresh(connection)
    assert auth_attempt.status == "CANCELLED" and connection.status == "ACTIVE"


def test_short_lease_rejected_before_claim_or_exchange(
    session, auth_attempt, monkeypatch, policy
):
    from unittest.mock import Mock

    from app.integrations.tiktok import auth

    policy.lease_ms = 50000
    remote = Mock()
    monkeypatch.setattr(auth, "_exchange_token", remote)
    with pytest.raises(DomainError) as error:
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=Mock(),
            policy=policy,
        )
    assert error.value.code == "admission_policy_invalid"
    assert auth_attempt.status == "PENDING" and auth_attempt.claimed_at is None
    remote.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "TIKTOK_APP_ID",
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
        "TIKTOK_AUTHORIZATION_URL",
    ],
)
def test_missing_application_configuration_is_guarded(
    session, auth_attempt, context, monkeypatch, field
):
    monkeypatch.setattr(settings, field, "")
    with pytest.raises(DomainError):
        start_authorization(
            session, context=context, connection_id=auth_attempt.connection_id
        )
    assert auth_attempt.status == "PENDING"


def test_slow_state_commit_cannot_call_after_lease_budget(
    session, auth_attempt, monkeypatch, redis_client, policy
):
    from unittest.mock import Mock

    from app.integrations.tiktok import auth

    remote = Mock()
    monkeypatch.setattr(auth, "_exchange_token", remote)
    ticks = iter([0.0, 100.0])
    monkeypatch.setattr(auth, "monotonic", lambda: next(ticks))
    with pytest.raises(DomainError) as error:
        finish_authorization(
            session,
            state="fake-state",
            auth_code="fake-code",
            now=datetime.now(UTC),
            redis_client=redis_client,
            policy=policy,
        )
    assert error.value.code == "oauth_result_unknown"
    remote.assert_not_called()
    session.refresh(auth_attempt)
    assert auth_attempt.status == "RESULT_UNKNOWN"
