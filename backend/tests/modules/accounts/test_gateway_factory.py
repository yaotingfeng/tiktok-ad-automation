"""Owned task gateways over actual adapters, PostgreSQL and Redis."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.accounts.connection_models import (
    ConnectionToolObservation,
)
from app.modules.accounts.models import (
    TikTokConnection,
)
from tests.integrations.tiktok.gateway_support import (  # noqa: F401
    business_calls,
    gateway,
    read_vbo,
)
from tests.integrations.tiktok.gateway_support import (
    database_engine as database_engine,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_case as gateway_case,
)
from tests.integrations.tiktok.gateway_support import (
    gateway_wire as gateway_wire,
)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_factory_uses_real_adapters_and_no_transaction_crosses_http(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert read_vbo(client, gateway_case).facts.vo_min_roas == "1.25"
        assert "synthetic-original-token" not in repr(client)
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1
    assert any(
        "synthetic-original-token" in token for token in gateway_wire["tokens"] if token
    )


def test_mcp_does_not_require_marketing_api_app(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch
):
    for name in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, name, "")
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert read_vbo(client, gateway_case).facts.vo_min_roas == "1.25"
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_runtime_cannot_enumerate_or_cross_bc(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError) as error:
            client.accounts.business_centers(page=1, page_size=50)
        assert error.value.code == "read_directory_forbidden"
        with pytest.raises(DomainError) as error:
            client.accounts.roles(bc_id="another-bc", page=1, page_size=50)
        assert error.value.code == "read_bc_mismatch"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_normal_rotation_before_factory_uses_new_material_same_route(
    database_engine, redis_client, gateway_case, gateway_wire
):
    from app.core.credentials import decrypt_credentials

    context, route, _ = gateway_case
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, route.connection_id)
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["access_token"] = "synthetic-rotated-token"
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=material
        )
        connection.credential_revision += 1
        session.add(connection)
        session.commit()
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert read_vbo(client, gateway_case).facts.vo_min_roas == "1.25"
    assert route.authorization_revision == 1
    assert any(
        "synthetic-rotated-token" in token for token in gateway_wire["tokens"] if token
    )
    assert not any(
        "synthetic-original-token" in token for token in gateway_wire["tokens"] if token
    )


def test_insufficient_token_expiry_queues_without_any_http(
    database_engine, redis_client, gateway_case, gateway_wire
):
    from app.core.credentials import decrypt_credentials

    context, route, _ = gateway_case
    with Session(database_engine) as session:
        connection = session.get(TikTokConnection, route.connection_id)
        material = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["expires_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=material
        )
        session.add(connection)
        session.commit()
    with pytest.raises(DomainError) as error:
        with gateway(database_engine, redis_client, gateway_case):
            pytest.fail("insufficient token admitted")
    assert error.value.code == "mcp_refresh_pending"
    assert gateway_wire["wire"].calls == []
    with Session(database_engine) as session:
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        ).one()
        assert dispatch.task_name == "accounts.refresh_mcp"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_api_requires_app_before_http(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch
):
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "")
    with pytest.raises(DomainError) as error:
        with gateway(database_engine, redis_client, gateway_case):
            pytest.fail("unconfigured API opened")
    assert error.value.code == "tiktok_app_incomplete"
    assert gateway_wire["sdk_calls"] == []


def test_mcp_requires_its_own_complete_tool_observation(
    database_engine, redis_client, gateway_case, gateway_wire
):
    _, route, _ = gateway_case
    with Session(database_engine) as session:
        session.exec(
            delete(ConnectionToolObservation).where(
                ConnectionToolObservation.connection_id == route.connection_id
            )
        )
        session.commit()
    with pytest.raises(DomainError) as error:
        with gateway(database_engine, redis_client, gateway_case):
            pytest.fail("unobserved tools admitted")
    assert error.value.code == "gateway_tool_observation_required"
    assert gateway_wire["wire"].calls == []
