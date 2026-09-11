"""P3.1 经实际任务工厂、PostgreSQL/Redis 授权准入读取广告。"""

# ruff: noqa: F811 -- reuse the real account/connection fixture and HTTP boundary

import pytest

from app.integrations.tiktok.contracts.builds import BuildReadQuery
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.modules.builds.readback_compare import compare_record
from app.modules.builds.request_compiler import decode_intent
from tests.contracts.test_tiktok_build_contract import build_bodies
from tests.integrations.tiktok.gateway_support import (  # noqa: F401
    business_calls,
    database_engine,
    gateway,
    gateway_case,
    gateway_wire,
)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "kind,id_key,operation",
    [
        ("CAMPAIGN", "campaign_id", "build.get_campaigns"),
        ("ADGROUP", "adgroup_id", "build.get_adgroups"),
        ("AD", "smart_plus_ad_id", "build.get_ads"),
        ("CTA", "creative_portfolio_id", "build.get_cta_portfolio"),
    ],
)
def test_factory_exposes_scoped_build_read(
    database_engine, redis_client, gateway_case, gateway_wire, kind, id_key, operation
):
    body = {**build_bodies()[kind], "advertiser_id": gateway_case[2]}
    intent = decode_intent(kind, body)
    row = {**body, id_key: "remote-1"}
    data = (
        row
        if kind == "CTA"
        else {
            "list": [row],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        }
    )
    gateway_wire["sdk_data"]["data"] = data
    tool = next(
        item.tool_name for item in load_tool_contracts() if item.operation == operation
    )
    gateway_wire["wire"].results[tool].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    query = BuildReadQuery(intent=intent, remote_id="remote-1")
    with gateway(database_engine, redis_client, gateway_case) as client:
        page = client.builds.read_page(query=query)
        assert compare_record(query=query, record=page.rows[0]) == "MATCH"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_factory_exposes_adgroup_status_supplement(
    database_engine, redis_client, gateway_case, gateway_wire
):
    advertiser = gateway_case[2]
    data = {
        "list": [
            {
                "advertiser_id": advertiser,
                "adgroup_id": "group-1",
                "operation_status": "ENABLE",
            }
        ],
        "page_info": {"page": 1, "page_size": 100, "total_number": 1, "total_page": 1},
    }
    gateway_wire["sdk_data"]["data"] = data
    tool = next(
        item.tool_name
        for item in load_tool_contracts()
        if item.operation == "build.get_regular_adgroups"
    )
    gateway_wire["wire"].results[tool].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    with gateway(database_engine, redis_client, gateway_case) as client:
        status = client.builds.read_adgroup_status(
            advertiser_id=advertiser, adgroup_id="group-1"
        )
        assert status.adgroup_id == "group-1" and status.operation_status == "ENABLE"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1
