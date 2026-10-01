"""管理适配器合同测试；传输边界使用内存替身，不连接 TikTok。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters import sdk_management
from app.integrations.tiktok.adapters.mcp_management import McpManagementOperations
from app.integrations.tiktok.adapters.sdk_management import (
    SdkManagementOperations,
    _payload,
)
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.integrations.tiktok.contracts.management import ManagementCommand


class _McpWire:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, *, operation, advertiser_id, arguments, **_kwargs):
        self.calls.append((operation, advertiser_id, arguments))
        return McpBusinessResponse({}, CallEvidence(request_id="management-request"))

    def has_contract(self, _operation):
        return True


def _command(field="material_status"):
    return ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "ad", "smart-ad-1"),
        field=field,
        original={"ad_type": "SMART_PLUS"},
        desired={"operation_status": "DISABLE"},
        ad_material_id="ad-material-1" if field == "material_status" else None,
    )


def _allow(_advertiser: str, _operation: str, _kind: str) -> None:
    return None


def test_mcp_material_status_uses_ad_reference():
    command = _command()
    assert _payload(command, smart_plus=True)["ad_material_ids"] == [command.ad_material_id]
    assert _payload(command, smart_plus=True)["smart_plus_ad_id"] == command.ref.remote_id
    wire = _McpWire()
    receipt = McpManagementOperations(wire, capability_check=_allow).apply(command)  # type: ignore[arg-type]
    assert receipt.outcome == "ACCEPTED"
    assert wire.calls[0][2]["advertiser_id"] == command.ref.advertiser_id
    assert wire.calls[0][2]["ad_material_ids"] == [command.ad_material_id]


def test_sdk_material_status_uses_ad_reference_and_capability_gate(monkeypatch):
    calls: list[dict] = []

    class FakeAsync:
        def get(self):
            return None

    class FakeAdApi:
        def __init__(self, _client):
            pass

        def smart_plus_ad_material_status_update(self, _token, **kwargs):
            calls.append(kwargs)
            return FakeAsync()

    class FakeClient:
        default_headers = {"Access-Token": "synthetic-token"}
        last_response = type("Response", (), {"data": b'{"code": 0, "data": {}}'})()

    @contextmanager
    def scope(*_args):
        yield

    monkeypatch.setattr(sdk_management.sdk, "AdApi", FakeAdApi)
    command = _command()
    receipt = SdkManagementOperations(
        FakeClient(),
        request_scope=scope,
        management_scope=scope,
        capability_check=_allow,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    ).apply(command)
    assert receipt.outcome == "ACCEPTED"
    assert calls[0]["body"]["smart_plus_ad_id"] == command.ref.remote_id
    assert calls[0]["body"]["ad_material_ids"] == [command.ad_material_id]

    def deny(_advertiser: str, _operation: str, _kind: str) -> None:
        raise DomainError("management_permission_unverified", "management permission unverified")

    with pytest.raises(DomainError, match="management permission"):
        SdkManagementOperations(
            FakeClient(),
            request_scope=scope,
            management_scope=scope,
            capability_check=deny,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        ).apply(command)
    assert len(calls) == 1


def test_conflicting_frozen_and_desired_types_are_zero_send_on_both_channels():
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "adgroup", "group-1"),
        field="budget",
        original={"ad_type": "REGULAR"},
        desired={"ad_type": "SMART_PLUS", "budget": "100"},
    )
    wire = _McpWire()
    with pytest.raises(DomainError, match="广告类型标识冲突"):
        McpManagementOperations(wire, capability_check=_allow).apply(command)  # type: ignore[arg-type]
    assert wire.calls == []

    scope_calls: list[tuple] = []

    @contextmanager
    def scope(*args):
        scope_calls.append(args)
        yield

    callback_calls: list[tuple[str, str, str]] = []

    def check(advertiser: str, operation: str, kind: str) -> None:
        callback_calls.append((advertiser, operation, kind))

    with pytest.raises(DomainError, match="广告类型标识冲突"):
        SdkManagementOperations(
            object(),
            request_scope=scope,
            management_scope=scope,
            capability_check=check,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        ).apply(command)
    assert callback_calls == [("adv-1", "update_budget", "adgroup")]
    assert scope_calls == []


def test_management_gate_runs_before_mcp_transport():
    wire = _McpWire()

    def deny(_advertiser, _operation, _kind):
        raise DomainError("management_permission_unverified", "management permission unverified")

    with pytest.raises(DomainError, match="management permission"):
        McpManagementOperations(wire, capability_check=deny).apply(_command())  # type: ignore[arg-type]
    assert wire.calls == []


def test_regular_group_replacement_preserves_required_fields():
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "adgroup", "group-1"),
        field="roas",
        original={"ad_type": "REGULAR", "adgroup_name": "name", "campaign_id": "campaign-1", "bid_type": "BID"},
        desired={"roas_bid": "1.2500"},
    )
    payload = _payload(command, smart_plus=False)
    assert payload["roas_bid"] == "1.2500"
    assert payload["campaign_id"] == "campaign-1"
    assert payload["adgroup_name"] == "name"


def test_missing_type_fails_closed_before_transport():
    wire = _McpWire()
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "ad", "ad-1"),
        field="status",
        original={},
        desired={"status": "DISABLE"},
    )
    with pytest.raises(DomainError, match="广告类型"):
        McpManagementOperations(wire, capability_check=_allow).apply(command)  # type: ignore[arg-type]
    assert wire.calls == []


def test_campaign_roas_is_unsupported_without_transport():
    wire = _McpWire()
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "campaign", "campaign-1"),
        field="roas",
        original={"ad_type": "SMART_PLUS"},
        desired={"roas_bid": "1.20"},
    )
    with pytest.raises(DomainError, match="不支持"):
        McpManagementOperations(wire, capability_check=_allow).apply(command)  # type: ignore[arg-type]
    assert wire.calls == []


def test_mcp_without_observed_management_contract_is_unsupported():
    class UnobservedWire(_McpWire):
        def has_contract(self, _operation):
            return False

    wire = UnobservedWire()
    with pytest.raises(DomainError, match="MCP 管理工具合同"):
        McpManagementOperations(wire, capability_check=_allow).apply(_command())  # type: ignore[arg-type]
    assert wire.calls == []


def test_mcp_without_observation_api_fails_closed_before_transport():
    class UnobservedWire:
        def __init__(self):
            self.calls = []

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return McpBusinessResponse({}, CallEvidence(request_id="unexpected"))

    wire = UnobservedWire()
    with pytest.raises(DomainError, match="MCP 管理工具合同"):
        McpManagementOperations(wire, capability_check=_allow).apply(_command())  # type: ignore[arg-type]
    assert wire.calls == []


@pytest.mark.parametrize("desired", [{"status": "PAUSE"}, {"status": None}, {"status": "ENABLE", "operation_status": "DISABLE"}])
def test_status_contract_rejects_invalid_or_ambiguous_values(desired):
    with pytest.raises(ValueError):
        ManagementCommand(
            ref=EntityRef(uuid4(), "adv-1", "ad", "ad-1"),
            field="status",
            original={"ad_type": "SMART_PLUS"},
            desired=desired,
        )


def test_management_adapters_require_capability_callback_at_construction():
    wire = _McpWire()
    with pytest.raises(DomainError, match="能力门禁"):
        McpManagementOperations(wire)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="能力门禁"):
        SdkManagementOperations(
            object(),
            request_scope=lambda *_args: None,  # type: ignore[arg-type]
            deadline=datetime.now(UTC),
        )
    with pytest.raises(DomainError, match="专用准入门禁"):
        SdkManagementOperations(
            object(),
            request_scope=lambda *_args: None,  # type: ignore[arg-type]
            capability_check=_allow,
            deadline=datetime.now(UTC),
        )


@pytest.mark.parametrize("smart_plus", [False, True])
def test_campaign_budget_uses_campaign_payload_contract(smart_plus):
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "campaign", "campaign-1"),
        field="budget",
        original={"ad_type": "SMART_PLUS" if smart_plus else "REGULAR"},
        desired={"budget": "100.00"},
    )
    payload = _payload(command, smart_plus=smart_plus)
    assert payload == {
        "advertiser_id": "adv-1",
        "campaign_id": "campaign-1",
        "budget": "100.00",
    }


@pytest.mark.parametrize("smart_plus", [False, True])
def test_sdk_campaign_budget_uses_matching_campaign_update(monkeypatch, smart_plus):
    calls: list[dict] = []
    scope_calls: list[tuple] = []

    class FakeAsync:
        def get(self):
            return None

    class FakeCampaignApi:
        def __init__(self, _client):
            pass

        def campaign_update(self, _token, **kwargs):
            calls.append({"method": "campaign_update", **kwargs})
            return FakeAsync()

        def smart_plus_campaign_update(self, _token, **kwargs):
            calls.append({"method": "smart_plus_campaign_update", **kwargs})
            return FakeAsync()

    class FakeAdgroupApi:
        def __init__(self, _client):
            pass

    class FakeClient:
        default_headers = {"Access-Token": "synthetic-token"}
        last_response = type("Response", (), {"data": b'{"code": 0, "data": {}}'})()

    @contextmanager
    def scope(*args):
        scope_calls.append(args)
        yield

    monkeypatch.setattr(sdk_management.sdk, "CampaignCreationApi", FakeCampaignApi)
    monkeypatch.setattr(sdk_management.sdk, "AdgroupApi", FakeAdgroupApi)
    command = ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "campaign", "campaign-1"),
        field="budget",
        original={"ad_type": "SMART_PLUS" if smart_plus else "REGULAR"},
        desired={"budget": "100.00"},
    )
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    receipt = SdkManagementOperations(
        FakeClient(),
        request_scope=scope,
        management_scope=scope,
        capability_check=_allow,
        deadline=deadline,
    ).apply(command)
    assert receipt.outcome == "ACCEPTED"
    assert calls[0]["method"] == (
        "smart_plus_campaign_update" if smart_plus else "campaign_update"
    )
    assert scope_calls == [("adv-1", "update_budget", "campaign", deadline)]
