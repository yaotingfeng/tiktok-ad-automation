"""官方 MCP 账户目录；BoundMCPClient 唯一拥有物理请求准入。"""

from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import (
    AuthorizationFacts,
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.contracts.discovery import (
    ObservedAuthorization,
)
from app.integrations.tiktok.discovery_read import DiscoveryReadAdapter

from .protocol import OFFICIAL_ENDPOINT, OFFICIAL_ISSUER
from .transport import BoundMCPClient


class McpAccountsGateway(DiscoveryReadAdapter):
    def __init__(
        self,
        client: BoundMCPClient,
        *,
        context: RuntimeReadContext | CandidateReadContext,
        authorization: AuthorizationFacts,
        observation_authorization: AuthorizationFacts | None = None,
    ):
        super().__init__(context=context, authorization=authorization)
        if (
            authorization.issuer != OFFICIAL_ISSUER
            or authorization.resource != OFFICIAL_ENDPOINT
        ):
            raise DomainError("mcp_authorization_mismatch", "授权服务与 MCP 连接不一致")
        self._client = client
        self._observation_authorization = observation_authorization

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        return self._client.call(
            operation=operation, advertiser_id=None, arguments=arguments
        )

    def observe_authorization(self) -> ObservedAuthorization:
        """仅由实际 user_info_get 结果观察主体，不把工具可见性变成权限。"""
        from dataclasses import replace
        from datetime import UTC, datetime

        from app.integrations.tiktok.contracts.discovery import ObservedAuthorization
        from app.integrations.tiktok.read_normalization import object_data

        observation = self._observation_authorization or self._authorization
        if self._observation_authorization is not None and (
            observation.evidence_source != "MCP_TOKEN_SCOPE_WITH_CLIENT"
            or not observation.scopes
        ):
            raise DomainError(
                "gateway_authorization_invalid", "当前材料授权范围或注册身份尚未核实"
            )
        response = self._call("accounts.authorization_facts", {})
        subject = object_data(response).get("core_user_id")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
            raise DomainError(
                "unsupported_account_schema", "授权用户结构缺少可核实主体"
            )
        facts = replace(
            observation,
            subject_id=subject,
            grant_id=None,
            read_authorized=None,
            # 实际 token 的 MCP scope 允许请求已接入的素材及广告工具；账户仍须
            # 完整目录与 ADMIN/OPERATOR 角色校验，不以工具可见性授予操作权。
            upload_authorized=True if "mcp:tt4b" in observation.scopes else None,
            build_authorized=True if "mcp:tt4b" in observation.scopes else None,
            evidence_source="MCP_USER_INFO_AND_TOKEN",
            observed_at=datetime.now(UTC),
        )
        return ObservedAuthorization(facts=facts, evidence=response.evidence)
