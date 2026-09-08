"""Pinned official SDK GET calls with public-document parameter/response proofs.

Admission and the worker hard deadline are enforced by scene.refresh_scene_context.
No generic path/method/query input and no write method exists here.
"""

import json
from hashlib import sha256
from typing import Any

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import checked_data

from .scene_schemas import SceneResource

SDK_REVISION = "f809c396520df2d7b201a9ccc5378d822b728ed3"
CONTRACT_REVISION = "minis-docs-2026-09-09-v3"
ENDPOINTS = {
    "account_roles": "/open_api/v1.3/bc/asset/get/",
    "identity": "/open_api/v1.3/identity/get/",
    "minis": "/open_api/v1.3/minis/get/",
    "cta": "/open_api/v1.3/creative/cta/recommend/",
    "vbo": "/open_api/v1.3/tool/vbo_status/",
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


def _invalid() -> DomainError:
    return DomainError("scene_response_unverified", "场景返回缺少可核实信息")


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise _invalid()
    return value


def parse_page(
    response: object,
    *,
    resource: SceneResource,
    page: int,
    advertiser_id: str,
    bc_id: str,
    minis_id: str | None,
) -> tuple[dict[str, Any], bool, str | None]:
    data = checked_data(response)
    raw_request_id = (
        response.get("request_id")
        if isinstance(response, dict)
        else getattr(response, "request_id", None)
    )
    request_id = (
        raw_request_id
        if isinstance(raw_request_id, str) and len(raw_request_id) <= 128
        else None
    )
    if resource == "cta":
        values = data.get("recommend_assets")
        if not isinstance(values, list) or len(values) > 50:
            raise _invalid()
        ids = set()
        assets = []
        for item in values:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("asset_ids"), list)
                or not 1 <= len(item["asset_ids"]) <= 50
            ):
                raise _invalid()
            for value in item["asset_ids"]:
                ids.add(_string(value))
                if len(ids) > 50:
                    raise _invalid()
            assets.append(
                {
                    "asset_ids": sorted(set(item["asset_ids"])),
                    "asset_content": _string(item.get("asset_content")),
                }
            )
        if len(ids) > 50:
            raise _invalid()
        return {"asset_ids": sorted(ids), "recommend_assets": assets}, True, request_id
    if resource == "vbo":
        result: dict[str, Any] = {}
        for key in ("vo_status", "vo_min_roas", "roas_status_day0", "roas_status_day7"):
            if key in data:
                result[key] = _string(data[key])
        if not result:
            raise _invalid()
        return result, True, request_id
    values, info = (
        data.get("identity_list" if resource == "identity" else "list"),
        data.get("page_info"),
    )
    if (
        not isinstance(values, list)
        or len(values) > PAGE_SIZE
        or not isinstance(info, dict)
    ):
        raise _invalid()
    if (
        any(
            type(info.get(key)) is not int
            for key in ("page", "page_size", "total_page", "total_number")
        )
        or info["page"] != page
        or info["page_size"] != PAGE_SIZE
        or not 0 <= info["total_page"] <= 1000
        or info["total_number"] < 0
    ):
        raise _invalid()
    last = page >= max(1, info["total_page"])
    if page > max(1, info["total_page"]) or not last and not values:
        raise _invalid()
    matches = []
    id_hashes: set[str] = set()
    id_field = {
        "account_roles": "asset_id",
        "minis": "minis_id",
        "identity": "identity_id",
    }[resource]
    for item in values:
        if not isinstance(item, dict):
            raise _invalid()
        remote_id = _string(item.get(id_field))
        digest = sha256(remote_id.encode()).hexdigest()
        if digest in id_hashes:
            raise _invalid()
        id_hashes.add(digest)
        if resource == "account_roles":
            if remote_id == advertiser_id:
                if item.get("asset_type") != "ADVERTISER" or item.get(
                    "advertiser_role"
                ) not in {"ADMIN", "OPERATOR", "ANALYST"}:
                    raise _invalid()
                matches.append(
                    {"advertiser_id": remote_id, "role": item["advertiser_role"]}
                )
        elif resource == "minis":
            if remote_id == minis_id:
                regions = item.get("region_codes")
                if (
                    not isinstance(regions, list)
                    or not 1 <= len(regions) <= 300
                    or any(
                        not isinstance(region, str)
                        or len(region) != 2
                        or not region.isascii()
                        or not all("A" <= letter <= "Z" for letter in region)
                        for region in regions
                    )
                ):
                    raise _invalid()
                if item.get("minis_status") not in {"ACTIVE", "INACTIVE"} or item.get(
                    "minis_type"
                ) not in {"MINI_SERIES", "MINI_GAME"}:
                    raise _invalid()
                matches.append(
                    {
                        "minis_id": remote_id,
                        "status": item["minis_status"],
                        "type": item["minis_type"],
                        "regions": sorted(set(regions)),
                    }
                )
        else:
            if (
                item.get("identity_type") != "BC_AUTH_TT"
                or item.get("identity_authorized_bc_id") != bc_id
            ):
                raise _invalid()
            if (
                item.get("available_status") == "AVAILABLE"
                and item.get("can_push_video") is True
                and item.get("is_gpppa") is False
            ):
                matches.append(
                    {
                        "identity_id": remote_id,
                        "identity_type": "BC_AUTH_TT",
                        "identity_authorized_bc_id": bc_id,
                    }
                )
    return (
        {
            "matches": matches[:2],
            "item_id_hashes": sorted(id_hashes),
            "total_number": info["total_number"],
            "total_page": info["total_page"],
            "seen": len(values),
        },
        last,
        request_id,
    )
