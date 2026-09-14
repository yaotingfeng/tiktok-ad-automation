"""工厂拥有的官方 SDK 广告创建与只读适配；逐次授权/准入复用已有请求边界。"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.build_results import (
    created_result,
    sdk_creation_envelope,
)
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
from app.integrations.tiktok.group_isolation import (
    FrozenGroupIsolation,
    optimizer_rule_arguments,
    require_isolation_target,
)
from app.integrations.tiktok.official.accounts import (
    OfficialReadRequests,
    RequestScope,
    remaining,
)
from app.modules.builds.readback_compare import parse_page, parse_status
from app.modules.builds.request_compiler import (
    create_arguments,
    read_arguments,
    status_arguments,
)


class ApiBuildOperations:
    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        isolation: FrozenGroupIsolation | None = None,
    ):
        self._request_scope, self._deadline = request_scope, deadline
        self._isolation = isolation
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )

    def disable_adgroup(
        self, *, advertiser_id: str, adgroup_id: str
    ) -> McpBusinessResponse:
        require_isolation_target(
            self._isolation, advertiser_id=advertiser_id, adgroup_id=adgroup_id
        )
        client = self._requests.client
        sent = False
        try:
            remaining(self._deadline)
            with self._request_scope(
                advertiser_id, "build.disable_adgroup", self._deadline
            ):
                budget = remaining(self._deadline)
                sent = True
                sdk.AdgroupApi(client).smart_plus_adgroup_status_update(
                    client.default_headers["Access-Token"],
                    body={
                        "advertiser_id": advertiser_id,
                        "adgroup_ids": [adgroup_id],
                        "operation_status": "DISABLE",
                    },
                    async_req=True,
                    _request_timeout=(min(5.0, budget), min(30.0, budget)),
                ).get()
                return sdk_creation_envelope(json.loads(client.last_response.data))
        except RemoteCallError:
            raise
        except Exception:
            if not sent:
                raise
            # 停用失败也不能认为未发送；只允许后续读取状态，禁止盲重试。
            raise RemoteCallError(
                "group_isolation_result_unknown",
                effect="UNKNOWN",
                evidence=CallEvidence(),
            ) from None

    def list_optimizer_rules(
        self, *, advertiser_id: str, page: int = 1
    ) -> McpBusinessResponse:
        require_isolation_target(self._isolation, advertiser_id=advertiser_id)
        return self._read(
            "build.list_optimizer_rules",
            advertiser_id,
            optimizer_rule_arguments(advertiser_id=advertiser_id, page=page),
        )

    def create(self, *, attempt_id: UUID, intent: CreateIntent) -> CreatedObject:
        operation, arguments = create_arguments(
            attempt_id=attempt_id, intent=intent, channel="OFFICIAL_API"
        )
        client = self._requests.client
        methods = {
            "CAMPAIGN": sdk.CampaignCreationApi(client).smart_plus_campaign_create,
            "ADGROUP": sdk.AdgroupApi(client).smart_plus_adgroup_create,
            "AD": sdk.AdApi(client).smart_plus_ad_create,
            "CTA": sdk.CreativeManagementApi(client).creative_portfolio_create,
        }
        sent = False
        try:
            remaining(self._deadline)
            with self._request_scope(intent.advertiser_id, operation, self._deadline):
                budget = remaining(self._deadline)
                # async 官方入口保留业务 envelope；不使用会丢弃 code 的同步便捷层。
                # 不另设 future timeout，禁止请求线程仍在运行时释放共享准入。
                sent = True
                methods[intent.kind](
                    client.default_headers["Access-Token"],
                    body=arguments,
                    async_req=True,
                    _request_timeout=(min(5.0, budget), min(30.0, budget)),
                ).get()
                raw = json.loads(client.last_response.data)
                response = sdk_creation_envelope(raw)
                return created_result(kind=intent.kind, response=response)
        except RemoteCallError:
            raise
        except Exception:
            if not sent:
                raise
            # HTTP 已进入官方发送入口；非零业务码、解析/超时均不证明无副作用。
            raise RemoteCallError(
                "create_result_unknown", effect="UNKNOWN", evidence=CallEvidence()
            ) from None

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
            "build.list_optimizer_rules": sdk.AutomatedRulesApi(
                client
            ).optimizer_rule_list,
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
