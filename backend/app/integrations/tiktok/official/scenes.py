"""只接收工厂拥有的 SDK 客户端；固定场景操作，禁止任意 HTTP 网关。"""

import json
from datetime import datetime
from typing import Any

import business_api_client as sdk

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import RuntimeReadContext
from app.integrations.tiktok.contracts.scenes import ScenePage
from app.integrations.tiktok.read_normalization import scene_arguments, scene_page
from app.modules.builds.scene_schemas import SceneResource

from .accounts import OfficialReadRequests, RequestScope

ENDPOINTS = {
    "minis": "/open_api/v1.3/minis/get/",
    "cta": "/open_api/v1.3/creative/cta/recommend/",
    "vbo": "/open_api/v1.3/tool/vbo_status/",
    "regions": "/open_api/v1.3/tool/region/",
}


class OfficialScenesGateway:
    def __init__(
        self,
        client: Any,
        *,
        context: RuntimeReadContext,
        request_scope: RequestScope,
        deadline: datetime,
    ):
        if type(context) is not RuntimeReadContext:
            raise DomainError("read_context_invalid", "场景需要绑定 BC")
        self._context = context
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )

    def read_page(
        self,
        *,
        resource: SceneResource,
        advertiser_id: str,
        page: int,
        minis_id: str | None,
    ) -> ScenePage:
        operation, arguments = scene_arguments(
            resource=resource,
            advertiser_id=advertiser_id,
            bc_id=self._context.bc_id,
            page=page,
            minis_id=minis_id,
        )
        client = self._requests.client
        token = client.default_headers["Access-Token"]

        def send(timeout: tuple[float, float]) -> object:
            if resource == "account_roles":
                return sdk.BCApi(client).bc_asset_get(
                    access_token=token, _request_timeout=timeout, **arguments
                )
            if resource == "identity":
                return sdk.IdentityApi(client).identity_get(
                    access_token=token, _request_timeout=timeout, **arguments
                )
            # 固定版本生成方法缺少 Minis 参数；继续通过官方 ApiClient 发送固定 GET。
            query = [
                (key, json.dumps(value) if isinstance(value, (list, bool)) else value)
                for key, value in arguments.items()
            ]
            return client.call_api(
                ENDPOINTS[resource],
                "GET",
                {},
                query,
                {"Access-Token": token, "Accept": "application/json"},
                response_type="InlineResponse200",
                auth_settings=[],
                _return_http_data_only=True,
                _request_timeout=timeout,
            )

        response = self._requests.invoke(
            operation, arguments.get("advertiser_id"), send
        )
        return scene_page(
            response.data,
            evidence=response.evidence,
            resource=resource,
            page=page,
            advertiser_id=advertiser_id,
            bc_id=self._context.bc_id,
            minis_id=minis_id,
        )
