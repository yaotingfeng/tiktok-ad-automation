"""工厂拥有的官方 SDK 只读广告适配；逐次授权/准入复用已有请求边界。"""

from datetime import datetime
from typing import Any
from uuid import UUID

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import (
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
    CreatedObject,
    CreateIntent,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.official.accounts import OfficialReadRequests, RequestScope
from app.modules.builds.readback_compare import parse_page, parse_status
from app.modules.builds.request_compiler import read_arguments, status_arguments


class ApiBuildOperations:
    def __init__(self, client: Any, *, request_scope: RequestScope, deadline: datetime):
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )

    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject:
        # P3.3 之前只读里程碑不开放写入；attempt_id 不是上游幂等保证。
        raise RemoteCallError(
            "build_capability_not_enabled", effect="NOT_SENT", evidence=CallEvidence()
        )

    def _read(
        self, operation: str, advertiser_id: str, arguments: dict[str, object]
    ) -> McpBusinessResponse:
        client = self._requests.client
        methods = {
            "build.get_campaigns": sdk.CampaignCreationApi(
                client
            ).smart_plus_campaign_get,
            "build.get_adgroups": sdk.AdgroupApi(client).smart_plus_adgroup_get,
            "build.get_ads": sdk.AdApi(client).smart_plus_ad_get,
            "build.get_cta_portfolio": sdk.CreativeManagementApi(
                client
            ).creative_portfolio_get,
            "build.get_regular_adgroups": sdk.AdgroupApi(client).adgroup_get,
        }

        def send(timeout: tuple[float, float]) -> object:
            return methods[operation](
                access_token=client.default_headers["Access-Token"],
                _request_timeout=timeout,
                **arguments,
            )

        try:
            return self._requests.invoke(operation, advertiser_id, send)
        except DomainError as error:
            if error.code not in {"tiktok_response_error", "read_deadline_exceeded"}:
                raise
            raise RemoteCallError(
                "build_readback_unverified", effect="UNKNOWN", evidence=CallEvidence()
            ) from None

    def read_page(self, *, query: BuildReadQuery) -> BuildPage:
        operation, arguments = read_arguments(query)
        response = self._read(operation, query.intent.advertiser_id, arguments)
        return parse_page(query=query, response=response)

    def read_adgroup_status(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> AdGroupStatus:
        arguments = status_arguments(advertiser_id=advertiser_id, adgroup_id=adgroup_id)
        response = self._read("build.get_regular_adgroups", advertiser_id, arguments)
        return parse_status(
            advertiser_id=advertiser_id, adgroup_id=adgroup_id, response=response
        )
