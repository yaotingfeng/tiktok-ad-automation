"""固定纠正隔离合同；真实 PG/Redis 与官方 SDK/MCP 的 HTTP 边界替身。"""

# ruff: noqa: F811 -- shared real connection and HTTP fixtures

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.modules.accounts.connection_models import ConnectionToolObservation
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
def test_ordinary_gateway_cannot_disable_any_group(
    database_engine, redis_client, gateway_case, gateway_wire
):
    with gateway(database_engine, redis_client, gateway_case) as client:
        assert hasattr(client.builds, "disable_adgroup"), (
            "typed isolation method missing"
        )
        with pytest.raises(DomainError) as error:
            client.builds.disable_adgroup(
                advertiser_id=gateway_case[2], adgroup_id="original-group"
            )
        assert error.value.code == "group_isolation_authority_required"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


def isolation_case(database_engine, gateway_case, gateway_wire):
    from app.integrations.tiktok.group_isolation import (
        FrozenGroupIsolation,
        group_isolation_contract_revision,
        isolation_contracts,
    )

    context, route, account = gateway_case
    contracts = isolation_contracts()
    schemas = {
        c.tool_name: {"name": c.tool_name, "inputSchema": c.input_schema}
        for c in contracts
    }
    with Session(database_engine) as session:
        row = session.exec(
            select(ConnectionToolObservation).where(
                ConnectionToolObservation.connection_id == route.connection_id
            )
        ).one()
        row.tool_schemas = {**row.tool_schemas, **schemas}
        session.add(row)
        session.commit()
    gateway_wire["wire"].tools.extend(schemas.values())
    return FrozenGroupIsolation(
        ledger_id=uuid4(),
        route=route,
        advertiser_id=account,
        adgroup_id="original-group",
        contract_revision=group_isolation_contract_revision(),
    )


def isolation_gateway(
    database_engine, redis_client, case, scope, callback=lambda: None
):
    return open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=case[0],
        route=case[1],
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
        group_isolation=scope,
        before_isolation_write=callback,
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_isolation_disables_only_frozen_group_then_reads_actual_status(
    database_engine, redis_client, gateway_case, gateway_wire
):
    base_revision = load_mcp_protocol().schema_manifest_sha256
    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    calls = []
    gateway_wire["sdk_data"]["data"] = {}
    gateway_wire["wire"].results["smart_plus_adgroup_status_update"].append(
        {
            "content": [
                {
                    "type": "text",
                    "text": '{"code":0,"data":{},"request_id":"disable-receipt"}',
                }
            ],
        }
    )
    with isolation_gateway(
        database_engine,
        redis_client,
        gateway_case,
        scope,
        lambda: calls.append("claim-checked"),
    ) as client:
        receipt = client.builds.disable_adgroup(
            advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
        )
        assert receipt.evidence.request_id in {"disable-receipt", "synthetic-request"}
        # 更新成功 envelope 不冒充停用已核查；必须再获取精确 ID 的实际状态。
        data = {
            "list": [
                {
                    "advertiser_id": scope.advertiser_id,
                    "adgroup_id": scope.adgroup_id,
                    "operation_status": "DISABLE",
                }
            ],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        }
        gateway_wire["sdk_data"]["data"] = data
        gateway_wire["wire"].results["adgroup_get"].append(
            {"content": [], "structuredContent": {"code": 0, "data": data}}
        )
        status = client.builds.read_adgroup_status(
            advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
        )
        assert status.operation_status == "DISABLE"
    assert calls
    sent = business_calls(gateway_wire, gateway_case[1].channel)
    assert len(sent) == 2
    if gateway_case[1].channel == "OFFICIAL_MCP":
        assert sent[0]["params"]["name"] == "smart_plus_adgroup_status_update"
        assert sent[0]["params"]["arguments"] == {
            "advertiser_id": scope.advertiser_id,
            "adgroup_ids": ["original-group"],
            "operation_status": "DISABLE",
        }
    else:
        import json

        assert sent[0][1].endswith("/smart_plus/adgroup/status/update/")
        assert json.loads(sent[0][2]["body"]) == {
            "advertiser_id": scope.advertiser_id,
            "adgroup_ids": ["original-group"],
            "operation_status": "DISABLE",
        }
    assert load_mcp_protocol().schema_manifest_sha256 == base_revision


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "invalid", ["target", "account", "claim", "no_callback", "revision", "route"]
)
def test_isolation_scope_or_ledger_failure_prevents_send(
    database_engine, redis_client, gateway_case, gateway_wire, invalid
):
    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    account, target = scope.advertiser_id, scope.adgroup_id
    if invalid == "target":
        target = "unrelated-successful-group"
    if invalid == "account":
        account = "other-account"
    if invalid == "revision":
        scope = scope.model_copy(update={"contract_revision": "0" * 64})
    if invalid == "route":
        scope = scope.model_copy(
            update={"route": scope.route.model_copy(update={"binding_revision": 99})}
        )

    def guard():
        if invalid == "claim":
            raise DomainError("correction_claim_lost", "合成失效 claim")

    with pytest.raises(DomainError):
        with isolation_gateway(
            database_engine,
            redis_client,
            gateway_case,
            scope,
            None if invalid == "no_callback" else guard,
        ) as client:
            client.builds.disable_adgroup(advertiser_id=account, adgroup_id=target)
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_optimizer_rule_read_is_unfiltered_bounded_and_never_claims_completeness(
    database_engine, redis_client, gateway_case, gateway_wire
):
    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    data = {
        "list": [{"rule_id": "rule-1", "status": "ON", "action": "TURN_ON"}],
        "page_info": {"page": 2, "total_page": 3},
    }
    gateway_wire["sdk_data"]["data"] = data
    gateway_wire["wire"].results["optimizer_rule_list_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    with isolation_gateway(
        database_engine, redis_client, gateway_case, scope, None
    ) as client:
        response = client.builds.list_optimizer_rules(
            advertiser_id=scope.advertiser_id, page=2
        )
        assert response.data == data
        for bad_page in (0, True, 1.5, 1001):
            with pytest.raises(DomainError):
                client.builds.list_optimizer_rules(
                    advertiser_id=scope.advertiser_id, page=bad_page
                )
    sent = business_calls(gateway_wire, gateway_case[1].channel)
    assert len(sent) == 1
    if gateway_case[1].channel == "OFFICIAL_MCP":
        assert sent[0]["params"]["arguments"] == {
            "advertiser_id": scope.advertiser_id,
            "page": 2,
            "page_size": 100,
        }
    else:
        assert sent[0][1].endswith("/optimizer/rule/list/")


@pytest.mark.parametrize("changed", ["persisted", "live", "missing"])
def test_supplemental_schema_must_match_both_saved_and_live_catalog(
    database_engine, redis_client, gateway_case, gateway_wire, changed
):
    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    tool = "smart_plus_adgroup_status_update"
    if changed == "live":
        gateway_wire["wire"].tools = [
            {**item, "inputSchema": {"type": "object"}}
            if item["name"] == tool
            else item
            for item in gateway_wire["wire"].tools
        ]
    else:
        with Session(database_engine) as session:
            row = session.exec(
                select(ConnectionToolObservation).where(
                    ConnectionToolObservation.connection_id == scope.route.connection_id
                )
            ).one()
            values = dict(row.tool_schemas)
            if changed == "missing":
                values.pop(tool)
            else:
                values[tool] = {"name": tool, "inputSchema": {"type": "object"}}
            row.tool_schemas = values
            session.add(row)
            session.commit()
    with pytest.raises(DomainError) as error:
        with isolation_gateway(
            database_engine, redis_client, gateway_case, scope
        ) as client:
            client.builds.disable_adgroup(
                advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
            )
    assert error.value.code == "mcp_contract_changed"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_build_permission_revoked_between_admission_checks_never_sends(
    database_engine, redis_client, gateway_case, gateway_wire
):
    from app.modules.accounts.models import BCAccountAccess

    scope = isolation_case(database_engine, gateway_case, gateway_wire)

    def revoke():
        with Session(database_engine) as session:
            grant = session.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.connection_id == scope.route.connection_id
                )
            ).one()
            grant.can_build = False
            session.add(grant)
            session.commit()

    with pytest.raises(DomainError):
        with isolation_gateway(
            database_engine, redis_client, gateway_case, scope, revoke
        ) as client:
            client.builds.disable_adgroup(
                advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
            )
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize("failure", ["business", "malformed", "disconnect"])
def test_uncertain_disable_result_is_unknown_and_never_replayed(
    database_engine, redis_client, gateway_case, gateway_wire, failure
):
    from app.integrations.tiktok.contracts.common import RemoteCallError

    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    tool = "smart_plus_adgroup_status_update"
    if failure == "disconnect":
        gateway_wire["wire"].disconnects.add(tool)
    else:
        gateway_wire["wire"].results[tool].append(
            {
                "content": [],
                "structuredContent": {
                    "code": 40002 if failure == "business" else 0,
                    "data": {} if failure == "business" else [],
                    "request_id": "original-receipt",
                },
            }
        )
    with pytest.raises(RemoteCallError) as error:
        with isolation_gateway(
            database_engine, redis_client, gateway_case, scope
        ) as client:
            client.builds.disable_adgroup(
                advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
            )
    assert error.value.effect == "UNKNOWN"
    if failure == "business":
        assert error.value.evidence.remote_code == 40002
        assert error.value.evidence.request_id == "original-receipt"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_isolation_session_cannot_create_or_enable_objects(
    database_engine, redis_client, gateway_case, gateway_wire
):
    from app.modules.builds.request_compiler import decode_intent
    from tests.contracts.test_tiktok_build_contract import build_bodies

    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    intent = decode_intent(
        "CAMPAIGN", {**build_bodies()["CAMPAIGN"], "advertiser_id": scope.advertiser_id}
    )
    with isolation_gateway(
        database_engine, redis_client, gateway_case, scope
    ) as client:
        with pytest.raises(TypeError):
            client.builds.disable_adgroup(
                advertiser_id=scope.advertiser_id,
                adgroup_id=scope.adgroup_id,
                operation_status="ENABLE",
            )
        with pytest.raises(DomainError):
            client.builds.create(attempt_id=uuid4(), intent=intent)
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "invalid", ["ENABLE", "DELETE", "wrong_id", "extra_ids", "extra_field", "no_scope"]
)
def test_raw_supplemental_call_cannot_bypass_exact_disable_scope(
    database_engine, redis_client, gateway_case, gateway_wire, invalid
):
    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    arguments = {
        "advertiser_id": scope.advertiser_id,
        "adgroup_ids": [scope.adgroup_id],
        "operation_status": "DISABLE",
    }
    if invalid in {"ENABLE", "DELETE"}:
        arguments["operation_status"] = invalid
    if invalid == "wrong_id":
        arguments["adgroup_ids"] = ["unrelated-group"]
    if invalid == "extra_ids":
        arguments["adgroup_ids"] = [scope.adgroup_id, "unrelated-group"]
    if invalid == "extra_field":
        arguments["surprise"] = True
    gateway_wire["wire"].results["smart_plus_adgroup_status_update"].append(
        {"content": [], "structuredContent": {"code": 0, "data": {}}}
    )
    with isolation_gateway(
        database_engine,
        redis_client,
        gateway_case,
        None if invalid == "no_scope" else scope,
    ) as client:
        with pytest.raises(DomainError):
            client.builds._client.call(
                operation="build.disable_adgroup",
                advertiser_id=scope.advertiser_id,
                arguments=arguments,
            )
    assert business_calls(gateway_wire, gateway_case[1].channel) == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_isolation_write_cannot_bypass_admission_lease_deadline(
    database_engine, redis_client, gateway_case, gateway_wire, monkeypatch
):
    from app.core.config import settings

    scope = isolation_case(database_engine, gateway_case, gateway_wire)
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {"build.disable_adgroup": {"lease_ms": 1000}},
        },
    )
    with pytest.raises(DomainError) as error:
        with isolation_gateway(
            database_engine, redis_client, gateway_case, scope
        ) as client:
            client.builds.disable_adgroup(
                advertiser_id=scope.advertiser_id, adgroup_id=scope.adgroup_id
            )
    assert error.value.code == "admission_policy_invalid"
    assert business_calls(gateway_wire, gateway_case[1].channel) == []
