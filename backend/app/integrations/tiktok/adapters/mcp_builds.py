"""仅消费 P0 固定 build.* 映射与已校验 envelope，不建立额外 HTTP 层。"""

from uuid import UUID

from app.integrations.tiktok.adapters.build_results import created_result
from app.integrations.tiktok.contracts.builds import (
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    CreatedObject,
    CreateIntent,
)
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.integrations.tiktok.group_isolation import (
    FrozenGroupIsolation,
    optimizer_rule_arguments,
    require_isolation_target,
)
from app.integrations.tiktok.mcp.transport import BoundMCPClient
from app.modules.builds.readback_compare import parse_page, parse_status
from app.modules.builds.request_compiler import (
    create_arguments,
    read_arguments,
    status_arguments,
)


class McpBuildOperations:
    def __init__(
        self, client: BoundMCPClient, *, isolation: FrozenGroupIsolation | None = None
    ):
        self._client = client
        self._isolation = isolation

    def disable_adgroup(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> McpBusinessResponse:
        require_isolation_target(
            self._isolation, advertiser_id=advertiser_id, adgroup_id=adgroup_id
        )
        # 返回真实 envelope，不从请求合成状态；隔离完成必须另作精确 ID 回读。
        return self._client.call(
            operation="build.disable_adgroup",
            advertiser_id=advertiser_id,
            arguments={
                "advertiser_id": advertiser_id,
                "adgroup_ids": [adgroup_id],
                "operation_status": "DISABLE",
            },
        )

    def list_optimizer_rules(
        self, *, advertiser_id: str, page: int = 1
    ) -> McpBusinessResponse:
        require_isolation_target(self._isolation, advertiser_id=advertiser_id)
        return self._client.call(
            operation="build.list_optimizer_rules",
            advertiser_id=advertiser_id,
            arguments=optimizer_rule_arguments(advertiser_id=advertiser_id, page=page),
        )

    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject:
        operation, arguments = create_arguments(
            attempt_id=attempt_id, intent=intent, channel="OFFICIAL_MCP"
        )
        response = self._client.call(
            operation=operation, advertiser_id=intent.advertiser_id, arguments=arguments
        )
        return created_result(kind=intent.kind, response=response)

    def read_page(self, *, query: BuildReadQuery) -> BuildPage:
        operation, arguments = read_arguments(query)
        response = self._client.call(
            operation=operation,
            advertiser_id=query.intent.advertiser_id,
            arguments=arguments,
        )
        return parse_page(query=query, response=response)

    def read_adgroup_status(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> AdGroupStatus:
        response = self._client.call(
            operation="build.get_regular_adgroups",
            advertiser_id=advertiser_id,
            arguments=status_arguments(
                advertiser_id=advertiser_id, adgroup_id=adgroup_id
            ),
        )
        return parse_status(
            advertiser_id=advertiser_id, adgroup_id=adgroup_id, response=response
        )
