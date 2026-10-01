"""官方 MCP 广告管理写适配器；BoundMCPClient 负责工具 schema 和物理准入。"""

from collections.abc import Callable

from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    ManagementReceipt,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient

from .sdk_management import _payload, _smart_plus

CapabilityCheck = Callable[[str, str, str], None]


class McpManagementOperations:
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        capability_check: CapabilityCheck | None = None,
        authorization_check: CapabilityCheck | None = None,
        require_capability: CapabilityCheck | None = None,
    ):
        self._client = client
        self._check = capability_check or authorization_check or require_capability

    def apply(self, command: ManagementCommand) -> ManagementReceipt:
        if not isinstance(command, ManagementCommand):
            raise TypeError("management command required")
        if self._check is not None:
            self._check(command.ref.advertiser_id, command.operation, command.ref.kind)
        payload = _payload(command, smart_plus=_smart_plus(command))
        try:
            response = self._client.call(
                operation=f"management.{command.operation}",
                advertiser_id=command.ref.advertiser_id,
                arguments=payload,
            )
        except RemoteCallError as error:
            if error.effect == "NOT_SENT":
                return ManagementReceipt("NOT_SENT", error.evidence.request_id, True, error.evidence)
            return ManagementReceipt("UNKNOWN", error.evidence.request_id, False, error.evidence)
        request_id = response.evidence.request_id or response.evidence.mcp_request_id
        return ManagementReceipt("ACCEPTED", request_id, False, response.evidence)


McpManagementAdapter = McpManagementOperations
