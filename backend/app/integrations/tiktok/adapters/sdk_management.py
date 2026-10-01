"""官方 SDK 广告管理写适配器。

只在官方生成 SDK 的边界发送；普通对象和 Smart+ 使用不同方法与最小 body，
授权、额度和冻结路由检查由 request_scope 在每次物理请求前完成。
"""

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]
from business_api_client.rest import ApiException
from business_api_client.tiktok_business.tiktok_exceptions import TiktokSDKError
from urllib3.exceptions import HTTPError

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.build_results import safe_identifier
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.management import (
    MANAGEMENT_OPERATIONS,
    ManagementCommand,
    ManagementReceipt,
)
from app.integrations.tiktok.official.accounts import RequestScope, remaining

CapabilityCheck = Callable[[str, str, str], None]


def _smart_plus(command: ManagementCommand) -> bool:
    values = {**command.original, **command.desired}
    marker = values.get("ad_type", values.get("platform_ad_type", values.get("is_smart_plus")))
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, str):
        return marker.upper() in {"SMART_PLUS", "UPGRADED_SMART_PLUS", "SMART+", "SMARTPLUS"}
    # 当前目录默认使用 Smart+ 身份；明确 regular/auction 标记才走旧端点。
    return str(values.get("campaign_type", "")).upper() not in {"REGULAR", "AUCTION", "NORMAL"}


def _decimal(value: object, field: str) -> str:
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is str:
        try:
            parsed = Decimal(value)
        except Exception:
            raise DomainError("management_request_invalid", f"{field} 数值无效") from None
    else:
        raise DomainError("management_request_invalid", f"{field} 必须使用精确十进制")
    if not parsed.is_finite() or parsed <= 0:
        raise DomainError("management_request_invalid", f"{field} 必须为正数")
    return format(parsed, "f")


def _request_id(raw: object) -> str | None:
    return safe_identifier(raw) if isinstance(raw, str) else None


def _receipt(raw: object) -> ManagementReceipt:
    if not isinstance(raw, dict) or type(raw.get("code")) is not int:
        raise RemoteCallError("management_result_unknown", effect="UNKNOWN", evidence=CallEvidence())
    evidence = CallEvidence(
        request_id=_request_id(raw.get("request_id")),
        remote_code=raw["code"] if raw["code"] != 0 else None,
    )
    if raw["code"] == 0 and isinstance(raw.get("data"), dict):
        return ManagementReceipt("ACCEPTED", evidence.request_id, False, evidence)
    if raw["code"] != 0:
        return ManagementReceipt("REJECTED", evidence.request_id, False, evidence)
    raise RemoteCallError("management_result_unknown", effect="UNKNOWN", evidence=evidence)


def _payload(command: ManagementCommand, *, smart_plus: bool) -> dict[str, Any]:
    ref = command.ref
    if command.field == "material_status":
        if not smart_plus:
            raise DomainError("management_contract_unsupported", "普通广告不支持独立素材状态")
        return {
            "advertiser_id": ref.advertiser_id,
            "smart_plus_ad_id": ref.remote_id,
            "ad_material_ids": [command.ad_material_id],
            "operation_status": command.desired.get(
                "operation_status", command.desired.get("status")
            ),
        }
    if command.field == "status":
        key = f"{ref.kind}_ids"
        if smart_plus and ref.kind == "ad":
            key = "smart_plus_ad_ids"
        return {
            "advertiser_id": ref.advertiser_id,
            key: [ref.remote_id],
            "operation_status": command.desired.get(
                "operation_status", command.desired.get("status")
            ),
        }
    if ref.kind not in {"campaign", "adgroup"}:
        raise DomainError("management_contract_unsupported", "该对象不支持预算或 ROAS 修改")
    payload: dict[str, Any] = {"advertiser_id": ref.advertiser_id}
    payload["campaign_id" if ref.kind == "campaign" else "adgroup_id"] = ref.remote_id
    source = {**command.original, **command.desired}
    if command.field == "roas":
        payload["roas_bid"] = _decimal(
            command.desired.get("roas_bid", command.desired.get("roas")), "roas"
        )
    else:
        payload["budget"] = _decimal(command.desired.get("budget"), "budget")
    if not smart_plus and ref.kind == "adgroup":
        # 普通组更新是整体替换合同，保留平台验证过的必填字段。
        for name in (
            "adgroup_name", "campaign_id", "bid_type", "billing_event",
            "optimization_goal", "promotion_type", "schedule_type",
            "schedule_start_time", "targeting_spec",
        ):
            if name in source:
                payload[name] = source[name]
    return payload


class SdkManagementOperations:
    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        capability_check: CapabilityCheck | None = None,
        authorization_check: CapabilityCheck | None = None,
        require_capability: CapabilityCheck | None = None,
    ):
        self._client = client
        self._scope = request_scope
        self._deadline = deadline
        self._check = capability_check or authorization_check or require_capability

    def apply(self, command: ManagementCommand) -> ManagementReceipt:
        if not isinstance(command, ManagementCommand):
            raise TypeError("management command required")
        operation = f"management.{command.operation}"
        if operation not in {f"management.{name}" for name in MANAGEMENT_OPERATIONS}:
            raise DomainError("management_operation_invalid", "管理操作无效")
        if self._check is not None:
            self._check(command.ref.advertiser_id, command.operation, command.ref.kind)
        smart_plus = _smart_plus(command)
        payload = _payload(command, smart_plus=smart_plus)
        methods: dict[str, Any]
        if command.field == "material_status":
            methods = {"smart": sdk.AdApi(self._client).smart_plus_ad_material_status_update}
            method = methods["smart"]
        elif command.field == "status":
            if smart_plus:
                method = {
                    "campaign": sdk.CampaignCreationApi(self._client).smart_plus_campaign_status_update,
                    "adgroup": sdk.AdgroupApi(self._client).smart_plus_adgroup_status_update,
                    "ad": sdk.AdApi(self._client).smart_plus_ad_status_update,
                }[command.ref.kind]
            else:
                method = {
                    "campaign": sdk.CampaignCreationApi(self._client).campaign_status_update,
                    "adgroup": sdk.AdgroupApi(self._client).adgroup_status_update,
                    "ad": sdk.AdApi(self._client).ad_status_update,
                }[command.ref.kind]
        elif smart_plus:
            method = sdk.CampaignCreationApi(self._client).smart_plus_campaign_update if command.ref.kind == "campaign" else sdk.AdgroupApi(self._client).smart_plus_adgroup_update
        else:
            method = sdk.AdgroupApi(self._client).adgroup_update
        sent = False
        try:
            remaining(self._deadline)
            with self._scope(command.ref.advertiser_id, operation, self._deadline):
                budget = remaining(self._deadline)
                sent = True
                method(
                    self._client.default_headers["Access-Token"],
                    body=payload,
                    async_req=True,
                    _request_timeout=(min(5.0, budget), min(30.0, budget)),
                ).get()
                raw = json.loads(self._client.last_response.data)
                return _receipt(raw)
        except RemoteCallError:
            raise
        except (ApiException, TiktokSDKError, HTTPError, TimeoutError, ValueError, TypeError):
            if sent:
                raise RemoteCallError("management_result_unknown", effect="UNKNOWN", evidence=CallEvidence()) from None
            return ManagementReceipt("NOT_SENT", None, True)


# Naming follows the API/MCP adapter pair used by the existing build adapters.
ApiManagementOperations = SdkManagementOperations
