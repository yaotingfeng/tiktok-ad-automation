"""管理适配器合同测试；传输边界使用内存替身，不连接 TikTok。"""

from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.mcp_management import McpManagementOperations
from app.integrations.tiktok.adapters.sdk_management import _payload
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


@pytest.mark.parametrize("channel", ["api", "mcp"])
def test_material_status_uses_ad_reference(channel):
    command = _command()
    assert _payload(command, smart_plus=True)["ad_material_ids"] == [command.ad_material_id]
    assert _payload(command, smart_plus=True)["smart_plus_ad_id"] == command.ref.remote_id
    wire = _McpWire()
    receipt = McpManagementOperations(wire).apply(command)  # type: ignore[arg-type]
    assert channel == "mcp" or receipt.outcome == "ACCEPTED"
    assert receipt.outcome == "ACCEPTED"
    assert wire.calls[0][2]["advertiser_id"] == command.ref.advertiser_id
    assert wire.calls[0][2]["ad_material_ids"] == [command.ad_material_id]


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
        McpManagementOperations(wire).apply(command)  # type: ignore[arg-type]
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
        McpManagementOperations(wire).apply(command)  # type: ignore[arg-type]
    assert wire.calls == []


def test_mcp_without_observed_management_contract_is_unsupported():
    class UnobservedWire(_McpWire):
        def has_contract(self, _operation):
            return False

    wire = UnobservedWire()
    with pytest.raises(DomainError, match="MCP 管理工具合同"):
        McpManagementOperations(wire).apply(_command())  # type: ignore[arg-type]
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
