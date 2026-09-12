"""已有授权缺少新管理观测时，重新读取工具而不要求 OAuth。"""

import pytest
from sqlmodel import Session, delete, select

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionToolObservation,
)
from app.modules.accounts.models import TikTokConnection
from tests.modules.accounts.test_mcp_multibc_management import (  # noqa: F401
    app_config,
    bc_page,
    bind,
    directory,
)
from tests.modules.accounts.test_mcp_multibc_management import (
    candidate as candidate,
)
from tests.modules.accounts.test_mcp_multibc_management import (
    catalog_wire as catalog_wire,
)
from tests.modules.accounts.test_mcp_multibc_management import (
    committed_context as committed_context,
)
from tests.modules.accounts.test_mcp_multibc_management import (
    oauth_wire as oauth_wire,
)


@pytest.mark.parametrize("old_contract", [False, True])
def test_existing_authorization_reobserves_tools_without_new_oauth(
    committed_context,
    candidate,
    catalog_wire,
    redis_client,
    oauth_wire,
    old_contract,
):
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, connection_id)
        original = (
            connection.credential_ciphertext,
            connection.credential_revision,
            connection.authorization_revision,
        )
        binding = session.get(
            BCConnectionBinding, (connection.tenant_id, "bc-1", connection_id)
        )
        binding_revision = binding.revision
        if old_contract:
            connection.adapter_contract_revision = "previous-reviewed-contract"
        # 升级前仅存在与已接受候选关联的工具观察，不能把它伪造成新的目录快照。
        session.exec(
            delete(ConnectionToolObservation).where(
                ConnectionToolObservation.connection_id == connection_id,
                ConnectionToolObservation.candidate_attempt_id.is_(None),
            )
        )
    bc_page(catalog_wire, bcs=("bc-1", "bc-2"), total_number=2)
    rows = directory(committed_context, connection_id, redis_client)
    assert [item["bc_id"] for item in rows] == ["bc-1", "bc-2"]
    assert [item["connected"] for item in rows] == [True, False]
    assert len(oauth_wire.calls) == 1
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert original == (
            connection.credential_ciphertext,
            connection.credential_revision,
            connection.authorization_revision,
        )
        assert (
            connection.adapter_contract_revision
            == load_mcp_protocol().schema_manifest_sha256
        )
        assert (
            session.get(
                BCConnectionBinding, (connection.tenant_id, "bc-1", connection_id)
            ).revision
            == binding_revision
        )
        observations = session.exec(
            select(ConnectionToolObservation).where(
                ConnectionToolObservation.connection_id == connection_id,
                ConnectionToolObservation.candidate_attempt_id.is_(None),
            )
        ).all()
        assert len(observations) == 2
        assert {item.call_evidence["kind"] for item in observations} == {
            "MANAGEMENT_SCHEMA_OBSERVATION",
            "MANAGEMENT_DIRECTORY",
        }
        assert all(
            item.call_evidence["authorization_revision"] == 1 for item in observations
        )


def test_failed_live_contract_check_preserves_previous_connection_version(
    committed_context, candidate, catalog_wire, redis_client, oauth_wire
):
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, connection_id)
        connection.adapter_contract_revision = "previous-reviewed-contract"
        original = (connection.credential_ciphertext, connection.authorization_revision)
    for tool in catalog_wire.tools:
        if tool["name"] == "bc_get":
            tool["inputSchema"] = {"type": "object"}
    with pytest.raises(DomainError):
        directory(committed_context, connection_id, redis_client, refresh=True)
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        assert connection.adapter_contract_revision == "previous-reviewed-contract"
        assert (
            connection.credential_ciphertext,
            connection.authorization_revision,
        ) == original
    assert len(oauth_wire.calls) == 1
