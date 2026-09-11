"""官方 MCP 账户目录；BoundMCPClient 唯一拥有物理请求准入。"""

from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import (
    AuthorizationFacts,
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.read_normalization import AccountsReadAdapter

from .protocol import OFFICIAL_ENDPOINT, OFFICIAL_ISSUER
from .transport import BoundMCPClient


class McpAccountsGateway(AccountsReadAdapter):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        context: RuntimeReadContext | CandidateReadContext,
        authorization: AuthorizationFacts,
    ):
        super().__init__(context=context, authorization=authorization)
        if (
            authorization.issuer != OFFICIAL_ISSUER
            or authorization.resource != OFFICIAL_ENDPOINT
        ):
            raise DomainError("mcp_authorization_mismatch", "授权服务与 MCP 连接不一致")
        self._client = client

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        return self._client.call(
            operation=operation, advertiser_id=None, arguments=arguments
        )
