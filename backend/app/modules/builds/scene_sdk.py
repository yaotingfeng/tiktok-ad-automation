"""Pinned official SDK GET calls with public-document parameter/response proofs.

Admission and the worker hard deadline are enforced by scene_jobs.process_scene_job
(and the legacy standalone scene.refresh_scene_context diagnostic helper).
No generic path/method/query input and no write method exists here.
"""

import json
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError

# 保留旧调用入口；纯数据校验由双通道共用模块唯一拥有。
from app.integrations.tiktok.read_normalization import parse_page as parse_page

from .scene_schemas import SceneResource

SDK_REVISION = "f809c396520df2d7b201a9ccc5378d822b728ed3"
CONTRACT_REVISION = "minis-docs-2026-09-09-v4"
ENDPOINTS = {
    "account_roles": "/open_api/v1.3/bc/asset/get/",
    "identity": "/open_api/v1.3/identity/get/",
    "minis": "/open_api/v1.3/minis/get/",
    "cta": "/open_api/v1.3/creative/cta/recommend/",
    "vbo": "/open_api/v1.3/tool/vbo_status/",
    "regions": "/open_api/v1.3/tool/region/",
}
PAGE_SIZE = 50


def request_page(
    client: Any, *, resource: SceneResource, bc_id: str, advertiser_id: str, page: int
) -> object:
    if resource not in ENDPOINTS or type(page) is not int or not 1 <= page <= 1000:
        raise DomainError("scene_request_invalid", "场景读取参数无效")
    token = client.default_headers["Access-Token"]
    if resource == "account_roles":
        # No filtering: official doc binds this to the auth_code's actual user.
        return sdk.BCApi(client).bc_asset_get(
            bc_id,
            "ADVERTISER",
            token,
            page=page,
            page_size=PAGE_SIZE,
            _request_timeout=(5, 30),
        )
    if resource == "identity":
        # Page info is reliable only with identity_type specified (official doc).
        return sdk.IdentityApi(client).identity_get(
            advertiser_id,
            token,
            identity_type="BC_AUTH_TT",
            identity_authorized_bc_id=bc_id,
            page=page,
            page_size=PAGE_SIZE,
            _request_timeout=(5, 30),
        )
    if resource == "minis":
        query: list[tuple[str, Any]] = [
            ("advertiser_id", advertiser_id),
            ("page", page),
            ("page_size", PAGE_SIZE),
        ]
    elif resource == "cta":
        query = [
            ("advertiser_id", advertiser_id),
            ("new_version", "true"),
            ("objective_type", "APP_PROMOTION"),
            ("promotion_type", "MINI_APP"),
            ("placements", json.dumps(["PLACEMENT_TIKTOK"])),
            ("optimization_goal", "VALUE"),
        ]
    elif resource == "regions":
        # doc1737189539571713 explicitly includes MINIS and a complete country list.
        # Pinned ToolApi.tool_region omits app_promotion_type/promotion_type.
        query = [
            ("advertiser_id", advertiser_id),
            ("placements", json.dumps(["PLACEMENT_TIKTOK"])),
            ("objective_type", "APP_PROMOTION"),
            ("app_promotion_type", "MINIS"),
            ("promotion_type", "MINI_APP"),
            ("level_range", "TO_COUNTRY"),
            ("language", "en"),
        ]
    else:
        # The generated VBO method lacks documented
        # campaign_automation_type. Use its official ApiClient for this fixed GET.
        query = [
            ("advertiser_id", advertiser_id),
            ("objective_type", "APP_PROMOTION"),
            ("app_promotion_type", "MINIS"),
            ("promotion_type", "MINI_APP"),
            ("placements", json.dumps(["PLACEMENT_TIKTOK"])),
            ("campaign_automation_type", "UPGRADED_SMART_PLUS"),
            ("budget_optimize_on", "true"),
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
        _request_timeout=(5, 30),
    )
