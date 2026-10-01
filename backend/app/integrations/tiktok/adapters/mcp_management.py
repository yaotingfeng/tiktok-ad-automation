"""官方 MCP 广告管理写适配器；BoundMCPClient 负责工具 schema 和物理准入。"""

from collections.abc import Callable

from app.core.errors import DomainError
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
        if self._check is None:
            raise DomainError("management_permission_unverified", "管理请求缺少账户能力门禁")

    def apply(self, command: ManagementCommand) -> ManagementReceipt:
        if not isinstance(command, ManagementCommand):
            raise TypeError("management command required")
        self._check(command.ref.advertiser_id, command.operation, command.ref.kind)
        operation = f"management.{command.operation}"
        if not hasattr(self._client, "has_contract") or not self._client.has_contract(operation):
            raise DomainError("management_contract_unsupported", "MCP 管理工具合同尚未核验")
        payload = _payload(command, smart_plus=_smart_plus(command))
        try:
            response = self._client.call(
                operation=operation,
                advertiser_id=command.ref.advertiser_id,
                arguments=payload,
                entity_kind=command.ref.kind,
            )
        except RemoteCallError as error:
            if error.code == "mcp_contract_changed":
                raise DomainError("mcp_contract_changed", "MCP 管理工具合同已变化") from None
            if error.code == "mcp_arguments_invalid":
                raise DomainError("mcp_arguments_invalid", "MCP 管理参数无效") from None
            if error.code == "mcp_tool_unavailable":
                raise DomainError("management_contract_unsupported", "MCP 管理工具合同尚未核验") from None
            if error.effect == "NOT_SENT":
                return ManagementReceipt("NOT_SENT", error.evidence.request_id, True, error.evidence)
            return ManagementReceipt("UNKNOWN", error.evidence.request_id, False, error.evidence)
        request_id = response.evidence.request_id or response.evidence.mcp_request_id
        return ManagementReceipt("ACCEPTED", request_id, False, response.evidence)


McpManagementAdapter = McpManagementOperations
