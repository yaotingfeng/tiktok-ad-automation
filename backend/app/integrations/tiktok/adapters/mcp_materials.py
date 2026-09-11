"""MCP 素材只读：官方代码客户端拥有准入；未核实写入永不发送。"""

from typing import Any

from app.integrations.tiktok.contracts import materials as contracts
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient
from app.modules.materials.sdk_assets import MaterialReadAdapter


class MCPMaterialOperations(MaterialReadAdapter):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        preview_allowed_hosts: frozenset[str] = frozenset(),
    ):
        super().__init__(preview_allowed_hosts=preview_allowed_hosts)
        self._client = client

    def _call(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: contracts.RemoteCallBudget,
    ) -> McpBusinessResponse:
        return self._client.call(
            operation=operation,
            advertiser_id=advertiser_id,
            arguments=arguments,
            deadline=budget.deadline,
        )

    def upload_video_url(
        self, request: contracts.URLVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def upload_video_file(
        self, request: contracts.FileVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def upload_image_url(
        self, request: contracts.URLImageUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.ImageReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )
