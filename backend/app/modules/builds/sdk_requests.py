"""Frozen-intent compilation and direct pinned official Smart+ create methods.

No execution entry point is registered here. The executor must recheck the frozen
scene/authority, commit its attempt, and obtain App/endpoint/tenant/advertiser
admission before calling invoke_create in a bounded prefork task. Admission must
outlive the official future AND owned ApiClient cleanup. Never call this from an
ASGI event-loop thread. No request or auth token is stored or logged here.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError

CREATE_ENDPOINTS = {
    "campaign": "/open_api/v1.3/smart_plus/campaign/create/",
    "adgroup": "/open_api/v1.3/smart_plus/adgroup/create/",
    "ad": "/open_api/v1.3/smart_plus/ad/create/",
}
ID_KEYS = {"campaign": "campaign_id", "adgroup": "adgroup_id", "ad": "smart_plus_ad_id"}
PROTECTED = frozenset(
    {
        "advertiser_id",
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
        "budget",
        "budget_optimize_on",
        "roas_bid",
        "operation_status",
    }
)
PORTFOLIO_ENDPOINT = "/open_api/v1.3/creative/portfolio/create/"
# Application copy policy, not an asserted TikTok platform limit.
APPLICATION_COPY_MAX_CHARACTERS = 100


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def ad_assets(
    mappings: Sequence[dict[str, str]],
    *,
    text: str,
    url: str,
    identity: dict[str, str],
) -> dict[str, Any]:
    """One SP text, the whole group, and verified target-account video/cover IDs."""
    if not mappings:
        raise DomainError("empty_material_group", "素材组不能为空")
    if (
        len(mappings) > 50
        or not _nonempty(text)
        or not _nonempty(url)
        or set(identity)
        != {"identity_type", "identity_id", "identity_authorized_bc_id"}
        or identity.get("identity_type") != "BC_AUTH_TT"
        or not all(_nonempty(value) for value in identity.values())
    ):
        raise DomainError("invalid_build_request", "创意信息无效")
    if len(text) > APPLICATION_COPY_MAX_CHARACTERS:
        raise DomainError("copy_too_long", "应用文案策略最多允许 100 个字符")
    creatives = []
    for item in mappings:
        if not _nonempty(item.get("video_id")) or not _nonempty(item.get("image_id")):
            raise DomainError("target_asset_incomplete", "目标账户素材尚未核实")
        creatives.append(
            {
                "creative_info": {
                    **identity,
                    "ad_format": "SINGLE_VIDEO",
                    "video_info": {"video_id": item["video_id"]},
                    "image_info": [{"web_uri": item["image_id"]}],
                }
            }
        )
    return {
        "creative_list": creatives,
        "ad_text_list": [{"ad_text": text}],
        "landing_page_url_list": [{"landing_page_url": url}],
    }


def cta_portfolio(
    *, advertiser_id: str, assets: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Keep the recommended CTA text bound to its actual asset IDs."""
    if not _nonempty(advertiser_id) or not 1 <= len(assets) <= 50:
        raise DomainError("cta_unavailable", "缺少当前账户的 CTA 推荐证据")
    content = []
    seen: set[str] = set()
    for asset in assets:
        ids = asset.get("asset_ids")
        if (
            not _nonempty(asset.get("asset_content"))
            or not isinstance(ids, (tuple, list))
            or not 1 <= len(ids) <= 50
            or not all(_nonempty(value) for value in ids)
        ):
            raise DomainError("cta_unavailable", "CTA 推荐证据不完整")
        seen.update(ids)
        content.append(
            {"asset_ids": list(ids), "asset_content": asset["asset_content"]}
        )
    if len(seen) > 50:
        raise DomainError("cta_unavailable", "CTA 推荐证据超出支持范围")
    return {
        "advertiser_id": advertiser_id,
        "creative_portfolio_type": "CTA",
        "portfolio_content": content,
    }


@dataclass(frozen=True)
class RemoteCreated:
    remote_id: str
    request_id: str | None
    operation_status: str | None


class TikTokResponseError(DomainError):
    """Safe evidence. A nonzero code alone does NOT prove no external effect.

    remote_code=-1 means no trustworthy structured response. No exception text,
    raw SDK data, or remote message is preserved. The executor decides recovery.
    """

    def __init__(self, *, code: int, request_id: str | None, reason: str):
        super().__init__(reason, reason)
        self.remote_code = code
        self.request_id = request_id


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError("invalid_build_request", "搭建请求格式无效")
    try:
        result: dict[str, Any] = json.loads(json.dumps(value, allow_nan=False))
    except ValueError, TypeError, RecursionError:
        raise DomainError("invalid_build_request", "搭建请求格式无效") from None
    return result


def compile_request(
    kind: str, *, fixed: dict[str, Any], resolved: dict[str, Any]
) -> dict[str, Any]:
    if kind not in CREATE_ENDPOINTS:
        raise DomainError("invalid_build_kind", "搭建层级无效")
    if not isinstance(resolved, dict) or PROTECTED.intersection(resolved):
        raise DomainError("scene_overrides_frozen_fields", "场景不能覆盖已确认字段")
    body = {**_json_copy(resolved), **_json_copy(fixed), "operation_status": "ENABLE"}
    if kind == "campaign":
        body["budget_optimize_on"] = True
    if kind == "adgroup" and "budget" in body:
        raise DomainError("adgroup_budget_not_allowed", "广告组不能设置独立预算")
    return body


def _request_id(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        else None
    )


def invoke_create(client: Any, *, kind: str, body: dict[str, Any]) -> RemoteCreated:
    """Exactly one official create, no automatic retry or rollback/activation.

    async_req=True preserves the official InlineResponse200. The SDK synchronous
    convenience path discards error fields into exception text; we never parse
    that text. .get() intentionally has no separate timeout: returning while its
    thread still runs would permit premature admission release. The containing
    prefork process hard deadline must bound both wait and client cleanup.
    """
    if kind not in CREATE_ENDPOINTS:
        raise DomainError("invalid_build_kind", "搭建层级无效")
    if not isinstance(body, dict) or body.get("operation_status") != "ENABLE":
        raise DomainError("invalid_creation_status", "创建状态必须为启用")
    body = _json_copy(body)
    methods = {
        "campaign": sdk.CampaignCreationApi(client).smart_plus_campaign_create,
        "adgroup": sdk.AdgroupApi(client).smart_plus_adgroup_create,
        "ad": sdk.AdApi(client).smart_plus_ad_create,
    }
    try:
        response = methods[kind](
            client.default_headers["Access-Token"],
            body=body,
            async_req=True,
            _request_timeout=(5, 30),
        ).get()
    except Exception:
        # Network/SDK/parse failures may follow a successful remote write.
        raise TikTokResponseError(
            code=-1, request_id=None, reason="create_result_unknown"
        ) from None
    return _created(response, id_key=ID_KEYS[kind])


def invoke_portfolio(client: Any, *, body: dict[str, Any]) -> RemoteCreated:
    """One separately armed external effect; the same unknown-result rules apply."""
    canonical = cta_portfolio(
        advertiser_id=body.get("advertiser_id", ""),
        assets=body.get("portfolio_content", ()),
    )
    if canonical != body:
        raise DomainError("invalid_build_request", "CTA 请求必须使用已保存的推荐信息")
    try:
        response = (
            sdk.CreativeManagementApi(client)
            .creative_portfolio_create(
                client.default_headers["Access-Token"],
                body=canonical,
                async_req=True,
                _request_timeout=(5, 30),
            )
            .get()
        )
    except Exception:
        raise TikTokResponseError(
            code=-1, request_id=None, reason="create_result_unknown"
        ) from None
    return _created(response, id_key="creative_portfolio_id")


def _created(response: object, *, id_key: str) -> RemoteCreated:
    raw_code = getattr(response, "code", None)
    request_id = _request_id(getattr(response, "request_id", None))
    if type(raw_code) is not int:
        raise TikTokResponseError(
            code=-1, request_id=request_id, reason="create_result_unknown"
        )
    if raw_code != 0:
        raise TikTokResponseError(
            code=raw_code, request_id=request_id, reason="tiktok_create_rejected"
        )
    data = getattr(response, "data", None)
    remote_id = data.get(id_key) if isinstance(data, dict) else None
    if not isinstance(remote_id, str) or not remote_id.strip() or len(remote_id) > 255:
        raise TikTokResponseError(
            code=0, request_id=request_id, reason="create_result_unknown"
        )
    status = data.get("operation_status") if isinstance(data, dict) else None
    return RemoteCreated(
        remote_id,
        request_id,
        status if isinstance(status, str) and status in {"ENABLE", "DISABLE"} else None,
    )
