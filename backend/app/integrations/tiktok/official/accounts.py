"""每个物理 SDK 请求独立准入；硬任务期限由工厂/worker 负责。"""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Any

import business_api_client as sdk
from business_api_client.rest import ApiException
from business_api_client.tiktok_business.tiktok_exceptions import TiktokSDKError
from urllib3.exceptions import HTTPError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import (
    AuthorizationFacts,
    CandidateReadContext,
    RuntimeReadContext,
)
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.integrations.tiktok.read_normalization import AccountsReadAdapter
from app.integrations.tiktok.sdk import checked_data

RequestScope = Callable[[str | None, str, datetime], AbstractContextManager[None]]


def remaining(deadline: datetime) -> float:
    if (
        not isinstance(deadline, datetime)
        or deadline.tzinfo is None
        or deadline.utcoffset() is None
    ):
        raise DomainError("read_deadline_invalid", "读取期限无效")
    value = (deadline - datetime.now(UTC)).total_seconds()
    if value <= 0:
        raise DomainError("read_deadline_exceeded", "读取期限已到")
    return value


@contextmanager
def _strict_sdk_envelope(client: Any) -> Iterator[None]:
    # 固定 SDK 会把 code 经 int() 转换，再丢掉 code/message；必须在其
    # deserialize 入口检查原始 JSON，不能把已经转换的模型当原始证据。
    original = client.deserialize
    missing = object()
    instance_original = client.__dict__.get("deserialize", missing)

    def deserialize(response: Any, response_type: Any) -> Any:
        try:
            raw = json.loads(response.data)
        except ValueError, TypeError, AttributeError, RecursionError:
            raise DomainError(
                "tiktok_response_error", "TikTok 返回不支持的数据结构"
            ) from None
        if (
            type(raw) is not dict
            or type(raw.get("code")) is not int
            or (raw["code"] == 0 and type(raw.get("data")) is not dict)
        ):
            raise DomainError("tiktok_response_error", "TikTok 返回不支持的数据结构")
        # 整数非零业务码仍交由原 SDK 抛错，再由 invoke 的既有分支脱敏。
        return original(response, response_type)

    client.deserialize = deserialize
    try:
        yield
    finally:
        # 只包装本次同步发送；成功、失败与任务中断均恢复工厂拥有的客户端。
        if instance_original is missing:
            del client.deserialize
        else:
            client.deserialize = instance_original


class OfficialReadRequests:
    def __init__(self, client: Any, *, request_scope: RequestScope, deadline: datetime):
        remaining(deadline)
        self.client = client
        self._scope = request_scope
        self._deadline = deadline

    def invoke(
        self,
        operation: str,
        advertiser_id: str | None,
        send: Callable[[tuple[float, float]], object],
    ) -> McpBusinessResponse:
        remaining(self._deadline)
        with self._scope(advertiser_id, operation, self._deadline):
            budget = remaining(self._deadline)
            try:
                with _strict_sdk_envelope(self.client):
                    response = send((min(5.0, budget), min(30.0, budget)))
            except ApiException, TiktokSDKError, HTTPError:
                raise DomainError(
                    "tiktok_response_error", "TikTok 请求未成功"
                ) from None
            remaining(self._deadline)
            data = checked_data(response)
            request_id = (
                response.get("request_id")
                if isinstance(response, dict)
                else getattr(response, "request_id", None)
            )
            if type(request_id) is not str or len(request_id) > 128:
                request_id = None
            return McpBusinessResponse(data, CallEvidence(request_id=request_id))


class OfficialAccountsGateway(AccountsReadAdapter):
    def __init__(
        self,
        client: Any,
        *,
        context: RuntimeReadContext | CandidateReadContext,
        authorization: AuthorizationFacts,
        app_id: str,
        secret: str,
        request_scope: RequestScope,
        deadline: datetime,
    ):
        super().__init__(context=context, authorization=authorization)
        self._requests = OfficialReadRequests(
            client, request_scope=request_scope, deadline=deadline
        )
        self._app_id = app_id
        self._secret = secret

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        client = self._requests.client
        token = client.default_headers["Access-Token"]

        def send(timeout: tuple[float, float]) -> object:
            kwargs = {**arguments, "_request_timeout": timeout}
            if operation == "accounts.list_bcs":
                return sdk.BCApi(client).bc_get(token, **kwargs)
            if operation == "accounts.list_bc_assets":
                return sdk.BCApi(client).bc_asset_get(access_token=token, **kwargs)
            if operation == "accounts.list_authorized_advertisers":
                return sdk.AuthenticationApi(client).oauth2_advertiser_get(
                    self._app_id, self._secret, token, **kwargs
                )
            if operation == "accounts.get_advertisers":
                return sdk.AccountManagementApi(client).advertiser_info(
                    access_token=token, **kwargs
                )
            raise DomainError("read_operation_invalid", "读取操作无效")

        return self._requests.invoke(operation, None, send)
