from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.context import TenantContext
from app.core.errors import DomainError, domain_error_handler
from app.core.logging import log_fields
from app.core.pagination import Page


def test_context_cannot_change_tenant_and_logs_drop_secrets():
    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = uuid4()
    assert log_fields(
        {"tenant_id": str(context.tenant_id), "access_token": "secret"}
    ) == {"tenant_id": str(context.tenant_id)}


def test_logs_drop_untrusted_values_even_under_allowed_keys():
    assert (
        log_fields(
            {
                "tenant_id": {"secret": "credential"},
                "error_code": "credential",
                "duration_ms": float("nan"),
            }
        )
        == {}
    )


def test_page_validates_items_and_defaults_cursor():
    assert Page[int](items=[1, 2]).model_dump() == {
        "items": [1, 2],
        "next_cursor": None,
    }
    with pytest.raises(ValidationError):
        Page[int](items=["invalid"])


@pytest.mark.parametrize(
    "code,status",
    [
        ("tenant_forbidden", 403),
        ("resource_not_found", 404),
        ("dispatch_key_conflict", 409),
        ("dispatch_payload_invalid", 422),
        ("admission_unavailable", 503),
        ("unexpected_credential_value", 500),
    ],
)
def test_domain_error_uses_fixed_status_and_never_echoes_external_text(
    code, status, caplog
):
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)

    @app.get("/")
    def fail():
        raise DomainError(code, "an-arbitrary-credential-value", retryable=True)

    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == status
    assert set(response.json()) == {"code", "message", "retryable"}
    assert "an-arbitrary-credential-value" not in response.text + caplog.text
    assert "unexpected_credential_value" not in response.text + caplog.text
    assert response.json()["retryable"] is True


def test_fixed_api_prefix_and_oauth_schema(client):
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert (
        schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"][
            "password"
        ]["tokenUrl"]
        == "/api/login/access-token"
    )
    assert all(path.startswith("/api/") for path in schema["paths"])
    assert client.get("/api/utils/health-check/").json() is True


def test_invalid_bearer_is_unauthorized(client):
    response = client.get("/api/users/me", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_unknown_api_paths_do_not_fall_through_to_frontend(client):
    assert client.get("/api/missing-endpoint").status_code == 404
    assert client.get("/api/v1/utils/health-check/").status_code == 404
