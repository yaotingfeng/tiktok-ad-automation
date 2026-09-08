import json
import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import business_api_client
import pytest
from business_api_client.rest import ApiException
from sqlalchemy.exc import IntegrityError

from app.core.credentials import encrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok import auth
from app.integrations.tiktok.sdk import checked_data, sdk_client
from app.modules.accounts.models import AuthorizationAttempt, TikTokConnection
from app.modules.accounts.schemas import ConnectionPublic


def test_sdk_transport_uses_token_and_closes_every_resource(
    session, context, auth_attempt, sdk_transport, monkeypatch
):
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    connection.credential_ciphertext = encrypt_credentials(
        tenant_id=context.tenant_id, value={"access_token": "fake-token"}
    )
    session.flush()
    with sdk_client(session, context=context, connection_id=connection.id) as client:
        assert client.configuration.debug is False
        retry = client.rest_client.pool_manager.connection_pool_kw["retries"]
        assert retry.total == 0 and retry.redirect == 0
        clear = Mock(wraps=client.rest_client.pool_manager.clear)
        monkeypatch.setattr(client.rest_client.pool_manager, "clear", clear)
        business_api_client.AuthenticationApi(client).oauth2_advertiser_get(
            "app",
            "secret",
            client.default_headers["Access-Token"],
            _request_timeout=(5, 30),
        )
    assert "Access-Token" not in client.default_headers
    clear.assert_called_once()
    assert client.pool._state == "CLOSE"
    assert all(not thread.is_alive() for thread in client.pool._pool)
    assert sdk_transport[0][2]["headers"]["Access-Token"] == "fake-token"


def test_scope_rejects_foreign_connection(session, context, other_context):
    foreign = TikTokConnection(
        tenant_id=other_context.tenant_id,
        status="ACTIVE",
        credential_ciphertext="not-even-valid",
    )
    session.add(foreign)
    session.flush()
    with pytest.raises(DomainError) as error:
        with sdk_client(session, context=context, connection_id=foreign.id):
            pytest.fail("Foreign connection must not be opened")
    assert error.value.code == "connection_unavailable"


def test_sdk_exception_sanitized_and_token_removed(session, context, auth_attempt):
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.status = "ACTIVE"
    connection.credential_ciphertext = encrypt_credentials(
        tenant_id=context.tenant_id, value={"access_token": "fake-secret"}
    )
    session.flush()
    with pytest.raises(DomainError) as error:
        with sdk_client(
            session, context=context, connection_id=connection.id
        ) as client:
            raise ApiException(status=401, reason="fake-secret")
    assert error.value.code == "tiktok_response_error"
    assert "fake-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert "Access-Token" not in client.default_headers


def test_public_schema_never_serializes_credentials(session, auth_attempt):
    connection = session.get(TikTokConnection, auth_attempt.connection_id)
    connection.credential_ciphertext = "fake-ciphertext"
    public = ConnectionPublic.model_validate(connection).model_dump_json()
    assert "credential" not in public and "fake-ciphertext" not in public


def test_attempt_fk_cannot_reference_another_tenant(
    session, other_context, auth_attempt
):
    with pytest.raises(IntegrityError), session.begin_nested():
        attempt = AuthorizationAttempt(
            tenant_id=other_context.tenant_id,
            actor_id=other_context.actor_id,
            connection_id=auth_attempt.connection_id,
            state_hash="f" * 64,
            expires_at=datetime.now(UTC),
        )
        session.add(attempt)
        session.flush()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        Mock(to_dict=lambda: []),
        Mock(to_dict=lambda: {"code": False, "data": {}}),
        business_api_client.InlineResponse200(code=4, message="fake-secret", data={}),
        business_api_client.InlineResponse200(code=0, data=[]),
    ],
)
def test_bad_responses_are_sanitized(response):
    with pytest.raises(DomainError) as error:
        checked_data(response)
    assert "fake-secret" not in str(error.value)


def test_official_transport_serializes_and_deserializes_oauth(sdk_transport):
    token = auth._token_request(
        app_id="fake-app", secret="fake-secret", auth_code="fake-code"
    )
    assert token == {"access_token": "fake-token"}
    body = json.loads(sdk_transport[0][2]["body"])
    assert body == {
        "app_id": "fake-app",
        "secret": "fake-secret",
        "auth_code": "fake-code",
    }


def test_sdk_body_and_query_logs_disabled_even_with_debug_root(sdk_transport, caplog):
    caplog.set_level(logging.DEBUG)
    auth._token_request(app_id="fake-app", secret="fake-secret", auth_code="fake-code")
    assert len(sdk_transport) == 1
    assert "fake-token" not in caplog.text and "fake-secret" not in caplog.text
    assert logging.getLogger("business_api_client.rest").disabled
    assert logging.getLogger("urllib3.connectionpool").disabled


def test_official_sdk_business_error_is_not_a_successful_token(monkeypatch):
    from unittest.mock import Mock

    from urllib3.response import HTTPResponse

    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *_args, **_kwargs: HTTPResponse(
            body=b'{"code":40001,"message":"fake-secret","data":{}}', status=200
        ),
    )
    channel = Mock()
    auth._exchange_worker(channel, "fake-app", "fake-secret", "fake-code")
    channel.send.assert_called_once_with(("failed", "tiktok_response_error"))
    channel.close.assert_called_once()


def test_official_timeout_returns_only_unknown_code(monkeypatch):
    from urllib3.exceptions import ReadTimeoutError

    def timeout(*_args, **_kwargs):
        raise ReadTimeoutError(
            None, "https://example.test/?secret=fake-secret", "fake-token"
        )

    monkeypatch.setattr("urllib3.PoolManager.request", timeout)
    channel = Mock()
    auth._exchange_worker(channel, "fake-app", "fake-secret", "fake-code")
    channel.send.assert_called_once_with(("unknown", "oauth_result_unknown"))
