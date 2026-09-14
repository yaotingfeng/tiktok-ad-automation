"""标准管理刷新经真实 HTTP 保存实际补充 schema，不改冻结路由或授权代数。"""

# ruff: noqa: F811 -- reuse the real OAuth/catalog/committed database fixtures

from copy import deepcopy

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.group_isolation import isolation_contracts
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.integrations.tiktok.mcp_auth.management import current_observation
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import TikTokConnection
from tests.modules.accounts.test_mcp_multibc_management import (  # noqa: F401
    app_config,
    bc_page,
    bind,
    candidate,
    catalog_wire,
    committed_context,
    directory,
    oauth_wire,
)


@pytest.mark.parametrize(
    "drift", [None, "smart_plus_adgroup_status_update", "optimizer_rule_list_get"]
)
def test_standard_refresh_adds_only_actual_matching_supplementals_without_route_changes(
    committed_context, candidate, catalog_wire, redis_client, drift
):
    # 初次正常授权只观察旧的主合同；没有直接写入/篡改工具观察表。
    bc_page(catalog_wire)
    connection_id, _ = bind(committed_context, candidate, redis_client, ["bc-1"])
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        old = current_observation(
            session, context=committed_context, connection=connection
        )
        old_id, original_tools = old.id, set(old.tool_schemas)
        connection_before = connection.model_dump(mode="json")
        binding_before = (
            session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.connection_id == connection_id
                )
            )
            .one()
            .model_dump(mode="json")
        )
    supplemental_names = {"smart_plus_adgroup_status_update", "optimizer_rule_list_get"}
    assert not original_tools & supplemental_names
    # 只在远端 HTTP 替身的 tools/list 返回中增加工具，保留生产观测/核验/持久化。
    for contract in isolation_contracts():
        schema = deepcopy(contract.input_schema)
        schema["description"] = (
            "synthetic-sensitive-description https://signed.invalid/private"
        )
        if contract.tool_name == drift:
            schema["required"] = [*schema["required"], "unreviewed_field"]
        catalog_wire.tools.append({"name": contract.tool_name, "inputSchema": schema})
    catalog_wire.tools.append(
        {"name": "unreviewed_arbitrary_write", "inputSchema": {"type": "object"}}
    )
    bc_page(catalog_wire)
    before_calls = len(catalog_wire.calls)
    result = directory(committed_context, connection_id, redis_client, refresh=True)
    assert any(row["bc_id"] == "bc-1" for row in result)
    with Session(engine) as session:
        connection = session.get(TikTokConnection, connection_id)
        observed = current_observation(
            session, context=committed_context, connection=connection
        )
        assert observed.id != old_id
        assert observed.pagination_complete
        assert (
            observed.expected_contract_revision
            == load_mcp_protocol().schema_manifest_sha256
        )
        expected_names = supplemental_names - ({drift} if drift else set())
        assert set(observed.tool_schemas) == original_tools | expected_names
        assert len(observed.tool_schemas) == len(original_tools) + len(expected_names)
        assert "synthetic-sensitive-description" not in str(observed.tool_schemas)
        assert "signed.invalid" not in str(observed.tool_schemas)
        if drift:
            assert (
                observed.call_evidence["unavailable_tools"][drift]
                == "mcp_contract_changed"
            )
        assert connection.model_dump(mode="json") == connection_before
        assert (
            session.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.connection_id == connection_id
                )
            )
            .one()
            .model_dump(mode="json")
            == binding_before
        )
    calls = catalog_wire.calls[before_calls:]
    assert any(call["method"] == "tools/list" for call in calls)
    assert {
        call["params"]["name"] for call in calls if call["method"] == "tools/call"
    } == {"user_info_get", "bc_get"}
