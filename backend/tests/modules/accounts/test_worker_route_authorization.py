"""Current authority is rechecked for each physical request in a task session."""

# ruff: noqa: F811 -- fixtures are intentionally shared by the two gateway modules

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, delete, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.modules.accounts.connection_models import (
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.tenants.models import TenantMembership
from tests.integrations.tiktok.gateway_support import (  # noqa: F401
    business_calls,
    database_engine,
    gateway,
    gateway_case,
    gateway_wire,
    read_vbo,
)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change,expected",
    [
        ("membership", "tenant_forbidden"),
        ("disabled", "connection_unavailable"),
        ("authorization", "route_authorization_changed"),
        ("contract", "route_contract_changed"),
        ("scope", "account_access_denied"),
        ("grant", "account_access_denied"),
        ("stale", "route_evidence_stale"),
        ("credential", "gateway_credentials_changed"),
    ],
)
def test_current_authority_fences_next_request(
    database_engine, redis_client, gateway_case, gateway_wire, change, expected
):
    context, route, advertiser = gateway_case
    with gateway(database_engine, redis_client, gateway_case) as client:
        read_vbo(client, gateway_case)
        with Session(database_engine) as session:
            connection = session.get(TikTokConnection, route.connection_id)
            grant = session.get(
                BCAccountAccess,
                (context.tenant_id, route.bc_id, advertiser, route.connection_id),
            )
            if change == "membership":
                row = session.get(
                    TenantMembership, (context.tenant_id, context.actor_id)
                )
                row.active = False
                session.add(row)
            elif change == "disabled":
                connection.status = "DISABLED"
            elif change == "authorization":
                connection.authorization_revision += 1
            elif change == "contract":
                connection.adapter_contract_revision = "changed-contract"
            elif change == "credential":
                connection.credential_revision += 1
            elif change == "scope":
                row = session.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == route.connection_id
                    )
                ).one()
                row.scopes = []
                row.permission_summary = {
                    **row.permission_summary,
                    "read_authorized": False,
                }
                session.add(row)
            elif change == "grant":
                grant.authorized = False
            elif change == "stale":
                grant.checked_at = datetime.now(UTC) - timedelta(days=1)
            session.add_all([connection, grant])
            session.commit()
        with pytest.raises(DomainError) as failure:
            read_vbo(client, gateway_case)
        assert failure.value.code == expected
        if change == "credential":
            assert failure.value.retryable is True
    assert len(business_calls(gateway_wire, route.channel)) == 1
    if change == "credential":
        with gateway(database_engine, redis_client, gateway_case) as reopened:
            read_vbo(reopened, gateway_case)
        assert route.authorization_revision == 1
        assert len(business_calls(gateway_wire, route.channel)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_default_and_identical_evidence_refresh_do_not_change_task(
    database_engine, redis_client, gateway_case, gateway_wire
):
    context, route, advertiser = gateway_case
    with gateway(database_engine, redis_client, gateway_case) as client:
        read_vbo(client, gateway_case)
        with Session(database_engine) as session:
            session.exec(
                delete(BCDefaultRoute).where(
                    BCDefaultRoute.tenant_id == context.tenant_id
                )
            )
            grant = session.get(
                BCAccountAccess,
                (context.tenant_id, route.bc_id, advertiser, route.connection_id),
            )
            grant.checked_at = datetime.now(UTC)
            row = session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == route.connection_id
                )
            ).one()
            row.verified_at = datetime.now(UTC)
            session.add_all([grant, row])
            session.commit()
        read_vbo(client, gateway_case)
    assert len(business_calls(gateway_wire, route.channel)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_foreign_advertiser_never_sends(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(DomainError) as failure:
            client.scenes.read_page(
                resource="vbo", advertiser_id="345", page=1, minis_id=None
            )
        assert failure.value.code == "account_not_in_bc"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_account_evidence_allows_only_bc_role_recheck(
    database_engine, redis_client, gateway_case, gateway_wire
):
    context, route, _ = gateway_case
    with Session(database_engine) as session:
        session.exec(
            delete(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == route.connection_id
            )
        )
        session.commit()
    data = {
        "list": [],
        "page_info": {"page": 1, "page_size": 50, "total_page": 0, "total_number": 0},
    }
    gateway_wire["sdk_data"]["data"] = data
    asset_tool = next(
        contract.tool_name
        for contract in load_tool_contracts()
        if contract.operation == "accounts.list_bc_assets"
    )
    gateway_wire["wire"].results[asset_tool].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        facts = client.accounts.authorization_facts()
        assert (
            facts.read_authorized
            is facts.upload_authorized
            is facts.build_authorized
            is None
        )
        assert facts.evidence_source == "UNKNOWN" and facts.observed_at.year == 1970
        assert (
            client.accounts.roles(bc_id=route.bc_id, page=1, page_size=50).items == ()
        )
        with pytest.raises(DomainError) as failure:
            read_vbo(client, gateway_case)
        assert failure.value.code == "route_evidence_stale"
    assert len(business_calls(gateway_wire, route.channel)) == 1


def test_mcp_401_does_not_refresh_or_replay_business_call(
    database_engine, redis_client, gateway_case, gateway_wire
):
    gateway_wire["wire"].status = 401
    with gateway(database_engine, redis_client, gateway_case) as client:
        with pytest.raises(RemoteCallError) as failure:
            read_vbo(client, gateway_case)
        assert failure.value.effect == "UNKNOWN"
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 1


def test_cross_tenant_route_rejected_before_http(
    database_engine, redis_client, gateway_case, gateway_wire
):
    from dataclasses import replace

    context, route, advertiser = gateway_case
    foreign = replace(context, tenant_id=uuid4())
    with pytest.raises(DomainError) as failure:
        with gateway(database_engine, redis_client, (foreign, route, advertiser)):
            pytest.fail("foreign route opened")
    assert failure.value.code == "connection_tenant_mismatch"
    assert gateway_wire["wire"].calls == []
