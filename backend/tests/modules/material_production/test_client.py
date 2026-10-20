import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.modules.material_production.client import MaterialToolClient, ToolError


def client(handler):
    return MaterialToolClient("owp_test-key", transport=httpx.MockTransport(handler))


def test_provision_contract_and_no_redirect():
    body = {
        "companyName": "测试公司",
        "username": "test-account",
        "password": "test-password123",
        "uploadPlatform": "r2",
        "pushAdPlatform": True,
    }

    def respond(request):
        assert request.url.path == "/api/provisioning/users"
        assert request.headers["Authorization"] == "Bearer owp_test-key"
        assert request.headers["Idempotency-Key"] == "order-1"
        assert json.loads(request.content) == body
        assert "cookie" not in request.headers
        return httpx.Response(
            201,
            json={
                "company": {
                    "id": "company_1",
                    "name": "测试公司",
                    "deliverySettings": {
                        "uploadPlatform": "r2",
                        "pushAdPlatform": True,
                    },
                },
                "user": {"id": "user_1", "username": "test-account"},
                "apiKey": {
                    "id": "apikey_1",
                    "companyId": "company_1",
                    "kind": "company",
                },
                "key": "owk_secret",
                "replayed": False,
            },
        )

    result = client(respond).provision(body, idempotency_key="order-1")
    assert result.company_id == "company_1"
    assert result.api_key == "owk_secret"
    assert "owk_secret" not in repr(result)


def test_login_link_request_and_absolute_result():
    def respond(request):
        assert request.url.path == "/api/provisioning/login-links"
        assert json.loads(request.content) == {"username": "test-account"}
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(
            201,
            json={
                "loginUrl": "/api/login-links/consume?token=opaque-token",
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )

    result = client(respond).login_link("test-account")
    assert result.login_url == (
        "https://manjumt.gzjunbo.net/api/login-links/consume?token=opaque-token"
    )
    assert "opaque-token" not in repr(result)


@pytest.mark.parametrize(
    "url",
    [
        "//evil.example/api/login-links/consume?token=x",
        "https://evil.example/api/login-links/consume?token=x",
        "/api/login-links/consume?token=x&token=y",
        "/api/login-links/consume?token=x&redirect=https://evil.example",
        "/api/login-links/consume?token=",
        "/api/login-links/consume?token=x#leak",
        "/api/login-links/consume?token=x\n",
        "/api/login-links/other?token=x",
    ],
)
def test_rejects_untrusted_links(url):
    c = client(
        lambda _: httpx.Response(
            201,
            json={
                "loginUrl": url,
                "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )
    )
    with pytest.raises(ToolError) as error:
        c.login_link("test")
    assert error.value.code == "INVALID_RESPONSE"
    assert "token=" not in str(error.value)


def test_expired_link_is_rejected():
    c = client(
        lambda _: httpx.Response(
            201,
            json={
                "loginUrl": "/api/login-links/consume?token=secret",
                "expiresAt": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )
    )
    with pytest.raises(ToolError):
        c.login_link("test")


@pytest.mark.parametrize(
    "status,retryable",
    [
        (400, False),
        (401, False),
        (404, False),
        (409, False),
        (429, True),
        (500, True),
        (302, False),
    ],
)
def test_errors_never_include_upstream_secrets(status, retryable):
    c = client(
        lambda _: httpx.Response(
            status,
            json={"error": "UNKNOWN_SECRET_VALUE", "message": "password=secret"},
            headers={"Location": "https://evil.example"},
        )
    )
    with pytest.raises(ToolError) as error:
        c.login_link("test")
    assert error.value.retryable is retryable
    assert "secret" not in str(error.value).lower()
    assert "UNKNOWN_SECRET_VALUE" not in str(error.value)


def test_timeout_can_be_retried_without_exposing_request():
    def respond(request):
        raise httpx.ReadTimeout("secret credential", request=request)

    with pytest.raises(ToolError) as error:
        client(respond).login_link("test")
    assert error.value.retryable
    assert error.value.code == "UNAVAILABLE"
