from unittest.mock import Mock

from app.core.config import settings
from app.integrations.tiktok import auth
from app.jobs.admission import Admission


def test_callback_uses_bound_tenant_and_removes_secrets_from_redirect(
    client, auth_attempt, policy, sdk_transport, monkeypatch
):
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    monkeypatch.setattr(
        "app.integrations.tiktok.sdk.admit_call", lambda *a, **kw: Admission(True, 0)
    )
    monkeypatch.setattr(
        "app.integrations.tiktok.sdk.release_call", lambda *a, **kw: None
    )
    response = client.get(
        "/api/integrations/tiktok/callback",
        params={
            "state": "fake-state",
            "auth_code": "private-code",
            "redirect_uri": "https://evil.example",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        f"/tenants/{auth_attempt.tenant_id}/accounts?tab=connections&"
    )
    assert "authorization=CANDIDATE_READY" in response.headers["location"]
    assert "private-code" not in response.text + str(response.headers)
    assert "fake-state" not in str(response.headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(sdk_transport) == 1
    repeated = client.get(
        "/api/integrations/tiktok/callback",
        params={"state": "fake-state", "auth_code": "private-code"},
        follow_redirects=False,
    )
    assert repeated.status_code == 303
    assert "authorization=invalid_oauth_state" in repeated.headers["location"]
    assert len(sdk_transport) == 1


def test_callback_denial_retains_state_and_retry_after(
    client, auth_attempt, policy, monkeypatch
):
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    monkeypatch.setattr(
        "app.integrations.tiktok.sdk.admit_call",
        lambda *a, **kw: Admission(False, 1200),
    )
    exchange = Mock()
    monkeypatch.setattr(auth, "_exchange_token", exchange)
    response = client.get(
        "/api/integrations/tiktok/callback",
        params={"state": "fake-state", "auth_code": "private-code"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    assert "private-code" not in response.text
    assert auth_attempt.claimed_at is None
    exchange.assert_not_called()


def test_callback_cancellation_never_exchanges_code(client, auth_attempt, monkeypatch):
    exchange = Mock()
    monkeypatch.setattr(auth, "_exchange_token", exchange)
    response = client.get(
        "/api/integrations/tiktok/callback",
        params={
            "state": "fake-state",
            "error": "access_denied",
            "error_description": "private-data",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "authorization=CANCELLED" in response.headers["location"]
    assert "private-data" not in str(response.headers) + response.text
    assert auth_attempt.status == "CANCELLED"
    exchange.assert_not_called()


def test_invalid_state_is_private_business_error(client, monkeypatch):
    exchange = Mock()
    monkeypatch.setattr(auth, "_exchange_token", exchange)
    response = client.get(
        "/api/integrations/tiktok/callback",
        params={"state": "not-issued", "auth_code": "private-code"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_oauth_state"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "private-code" not in response.text
    exchange.assert_not_called()
