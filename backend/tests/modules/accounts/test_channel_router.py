"""双通道页面只读合同；只使用本地注册文件及隔离数据库。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.modules.accounts.connection_models import ConnectionAuthorization
from tests.modules.accounts.test_router import headers


@pytest.fixture
def local_mcp_registration(monkeypatch, tmp_path):
    profile = load_mcp_protocol()
    redirect = "https://tkada.example.test/api/integrations/tiktok/mcp/callback"
    path = tmp_path / "registration.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "synthetic-client-private-value",
                "issuer": profile.issuer,
                "resource": profile.resource,
                "token_endpoint_auth_method": "none",
                "redirect_uris": [redirect],
            }
        )
    )
    monkeypatch.setattr(settings, "MCP_CLIENT_REGISTRATION_REF", str(path))
    monkeypatch.setattr(settings, "MCP_REDIRECT_URI", redirect)


@pytest.mark.usefixtures("local_mcp_registration")
def test_channel_configuration_separates_api_and_mcp_without_private_values(
    client, context, monkeypatch
):
    for key in ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI"):
        monkeypatch.setattr(settings, key, "")
    response = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/configuration",
        headers=headers(context),
    )
    assert response.status_code == 200
    value = response.json()
    assert set(value) == {"channels"}
    channels = {row["kind"]: row for row in value["channels"]}
    assert channels["OFFICIAL_API"]["configured"] is False
    assert channels["OFFICIAL_MCP"] == {
        "kind": "OFFICIAL_MCP",
        "configured": True,
        "status": "READY",
        "code": None,
    }
    assert all(
        set(row) == {"kind", "configured", "status", "code"}
        for row in value["channels"]
    )
    assert "synthetic-client-private-value" not in response.text
    assert "redirect_uri" not in response.text


@pytest.mark.usefixtures("local_mcp_registration")
def test_mcp_configuration_requires_encryption_too(client, context, monkeypatch):
    monkeypatch.setattr(settings, "CONNECTION_ENCRYPTION_KEY", "")
    response = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/configuration",
        headers=headers(context),
    )
    mcp = next(
        row for row in response.json()["channels"] if row["kind"] == "OFFICIAL_MCP"
    )
    assert mcp["configured"] is False
    assert mcp["code"] == "connection_encryption_unconfigured"


def test_connection_directory_exposes_scoped_default_and_current_facts(
    client, session, account_access_case
):
    context, grant = account_access_case
    path = f"/api/tenants/{context.tenant_id}/tiktok/connections"
    response = client.get(path, params={"bc_id": grant.bc_id}, headers=headers(context))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["kind"] == "OFFICIAL_API"
    assert item["is_default"] is True
    assert item["binding_count"] == 1
    assert item["read_authorized"] is True
    assert "encrypted_credentials" not in item
    assert "scopes" not in item
    bc_response = client.get(
        f"/api/tenants/{context.tenant_id}/bcs", headers=headers(context)
    )
    assert bc_response.json()["items"][0]["default_connection_id"] == str(
        grant.connection_id
    )
    from sqlmodel import select

    facts = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.connection_id == grant.connection_id
        )
    ).one()
    facts.verified_at = datetime.now(UTC) - timedelta(days=1)
    session.add(facts)
    session.flush()
    later = client.get(
        path, params={"bc_id": grant.bc_id}, headers=headers(context)
    ).json()["items"][0]
    assert later["read_authorized"] is None
    assert later["upload_authorized"] is None
    assert later["build_authorized"] is None


def test_account_directory_does_not_borrow_other_connection_permissions(
    client, session, account_access_case
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    context, grant = account_access_case
    second = TikTokConnection(
        tenant_id=context.tenant_id, kind="OFFICIAL_MCP", status="ACTIVE"
    )
    session.add(second)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=grant.bc_id,
            connection_id=second.id,
            kind=second.kind,
        )
    )
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id=grant.bc_id,
            advertiser_id=grant.advertiser_id,
            connection_id=second.id,
            in_bc=True,
            authorized=True,
            active=True,
            permission_state="UNKNOWN",
            checked_at=datetime.now(UTC),
        )
    )
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/accounts"
    api = client.get(path, params={"bc_id": grant.bc_id}, headers=headers(context))
    assert api.status_code == 200 and api.json()["items"][0]["can_build"] is True
    mcp = client.get(
        path,
        params={"bc_id": grant.bc_id, "connection_id": str(second.id)},
        headers=headers(context),
    )
    assert mcp.status_code == 200
    item = mcp.json()["items"][0]
    assert item["can_build"] is item["can_upload"] is False
    assert item["availability"] == "PERMISSION_UNKNOWN"


@pytest.mark.parametrize("scopes,flag", [([], True), (["2"], "true")])
def test_http_write_projection_requires_real_scopes_and_boolean(
    client, session, account_access_case, scopes, flag
):
    from sqlmodel import select

    context, grant = account_access_case
    facts = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.connection_id == grant.connection_id
        )
    ).one()
    facts.scopes = scopes
    facts.permission_summary = {
        "read_authorized": True,
        "upload_authorized": flag,
        "build_authorized": flag,
    }
    session.add(facts)
    session.flush()
    root = f"/api/tenants/{context.tenant_id}"
    account = client.get(
        root + "/accounts", params={"bc_id": grant.bc_id}, headers=headers(context)
    ).json()["items"][0]
    assert account["can_build"] is account["can_upload"] is False
    connection = client.get(
        root + "/tiktok/connections", headers=headers(context)
    ).json()["items"][0]
    assert connection["build_authorized"] is None


def test_new_mcp_authorization_clears_prior_refresh_failure_projection(
    client, session, context
):
    from uuid import uuid4

    from app.modules.accounts.connection_models import (
        McpAuthorizationAttempt,
        McpRefreshAttempt,
    )
    from app.modules.accounts.models import DiscoveryRun, TikTokConnection

    now = datetime.now(UTC)
    profile = load_mcp_protocol()
    connection = TikTokConnection(
        tenant_id=context.tenant_id,
        kind="OFFICIAL_MCP",
        status="ACTIVE",
        authorization_revision=2,
        credential_revision=2,
    )
    session.add(connection)
    session.flush()
    attempt = McpAuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        issuer=profile.issuer,
        resource=profile.resource,
        redirect_uri="https://example.test/callback",
        state_hash=uuid4().hex,
        expires_at=now + timedelta(minutes=5),
        status="ACCEPTED",
    )
    session.add(attempt)
    session.flush()
    session.add_all(
        [
            DiscoveryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=connection.id,
                mcp_candidate_attempt_id=attempt.id,
                status="COMPLETE",
                completed_at=now,
            ),
            McpRefreshAttempt(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                base_credential_revision=0,
                base_authorization_revision=0,
                status="CANDIDATE_READY",
                error_code="mcp_refresh_reauth_required",
                created_at=now - timedelta(hours=1),
            ),
            ConnectionAuthorization(
                tenant_id=context.tenant_id,
                connection_id=connection.id,
                authorization_revision=2,
                mcp_authorization_attempt_id=attempt.id,
                scopes=["mcp:tt4b"],
                permission_summary={"read_authorized": True},
                source="MCP_COMPLETE_DIRECTORY_READ",
                verified_at=now,
            ),
        ]
    )
    session.flush()
    response = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/connections", headers=headers(context)
    )
    item = response.json()["items"][0]
    assert item["status"] == "ACTIVE" and item["authorization_status"] == "ACCEPTED"
    assert item["error_code"] is None
    assert item["refresh_status"] is None
    assert (
        datetime.fromisoformat(item["last_authorized_at"].replace("Z", "+00:00")) == now
    )
    # 同一授权的正常轮换只增加凭据修订，仍应展示刚完成的刷新。
    connection.credential_revision = 3
    session.add(connection)
    session.add(
        McpRefreshAttempt(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            base_credential_revision=2,
            base_authorization_revision=2,
            status="PUBLISHED",
            created_at=now + timedelta(seconds=1),
        )
    )
    session.flush()
    later = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/connections", headers=headers(context)
    ).json()["items"][0]
    assert later["refresh_status"] == "PUBLISHED"
    assert later["error_code"] is None
