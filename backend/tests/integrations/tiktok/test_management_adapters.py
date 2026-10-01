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


def _command(field="material_status"):
    return ManagementCommand(
        ref=EntityRef(uuid4(), "adv-1", "ad", "smart-ad-1"),
        field=field,
        original={},
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
