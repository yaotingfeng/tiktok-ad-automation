"""Pinned official GET methods and strict comparison of persisted create intent.

No credentials, arbitrary remote messages, or raw response bodies leave this
module. A caller must own a 45s prefork task and Redis admission through cleanup.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypeGuard, cast

import business_api_client as sdk  # type: ignore[import-untyped]

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import checked_data
from app.modules.builds.sdk_requests import _request_id

PAGE_SIZE = 100
ENDPOINTS = {
    "CAMPAIGN": "/open_api/v1.3/smart_plus/campaign/get/",
    "ADGROUP": "/open_api/v1.3/smart_plus/adgroup/get/",
    "AD": "/open_api/v1.3/smart_plus/ad/get/",
    "CTA": "/open_api/v1.3/creative/portfolio/get/",
    "ADGROUP_STATUS": "/open_api/v1.3/adgroup/get/",
}
ID_KEYS = {
    "CAMPAIGN": "campaign_id",
    "ADGROUP": "adgroup_id",
    "AD": "smart_plus_ad_id",
    "CTA": "creative_portfolio_id",
}
NAME_KEYS = {"CAMPAIGN": "campaign_name", "ADGROUP": "adgroup_name", "AD": "ad_name"}


class ReadbackError(DomainError):
    def __init__(self, code: str = "readback_response_unknown"):
        super().__init__(code, "远端结果尚无法完整核实")


@dataclass(frozen=True)
class ReadPage:
    rows: tuple[dict[str, Any], ...]
    page: int
    total_pages: int
    total_number: int
    request_id: str | None


def nonempty(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def read_page(
    client: Any,
    *,
    kind: str,
    body: dict[str, Any],
    remote_id: str | None,
    page: int = 1,
    status_only: bool = False,
) -> ReadPage:
    if kind not in ID_KEYS or not nonempty(body.get("advertiser_id")) or page < 1:
        raise ReadbackError("readback_intent_incomplete")
    if status_only and (kind != "ADGROUP" or not remote_id):
        raise ReadbackError("readback_intent_incomplete")
    token = client.default_headers["Access-Token"]
    kwargs: dict[str, Any] = {
        "advertiser_id": body["advertiser_id"],
        "access_token": token,
        "async_req": True,
        "_request_timeout": (5, 30),
    }
    if kind == "CTA":
        if not remote_id:
            raise ReadbackError("cta_unknown_id")
        kwargs["creative_portfolio_id"] = remote_id
        method = sdk.CreativeManagementApi(client).creative_portfolio_get
    else:
        filters: dict[str, Any] = {}
        if remote_id:
            filters[ID_KEYS[kind] + "s"] = [remote_id]
        elif kind in {"CAMPAIGN", "ADGROUP"}:
            if not nonempty(body.get(NAME_KEYS[kind])):
                raise ReadbackError("readback_intent_incomplete")
            filters[NAME_KEYS[kind]] = body[NAME_KEYS[kind]]
        # Parent scope is always explicit, including actual-ID queries.
        for field in {"ADGROUP": ("campaign_id",), "AD": ("adgroup_id",)}.get(kind, ()):
            if not nonempty(body.get(field)):
                raise ReadbackError("readback_intent_incomplete")
            filters[field + "s"] = [body[field]]
        kwargs.update(filtering=filters, page=page, page_size=PAGE_SIZE)
        method = {
            "CAMPAIGN": sdk.CampaignCreationApi(client).smart_plus_campaign_get,
            "ADGROUP": sdk.AdgroupApi(client).adgroup_get
            if status_only
            else sdk.AdgroupApi(client).smart_plus_adgroup_get,
            "AD": sdk.AdApi(client).smart_plus_ad_get,
        }[kind]
    try:
        response = method(**kwargs).get()
        data = checked_data(response)
    except Exception:
        raise ReadbackError() from None
    raw = response if isinstance(response, dict) else response.to_dict()
    request_id = _request_id(raw.get("request_id"))
    if kind == "CTA":
        return ReadPage((data,), 1, 1, 1, request_id)
    rows, info = data.get("list"), data.get("page_info")
    if (
        not isinstance(rows, list)
        or len(rows) > PAGE_SIZE
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(info, dict)
    ):
        raise ReadbackError()
    values = [
        info.get(key) for key in ("page", "page_size", "total_number", "total_page")
    ]
    if any(type(value) is not int for value in values):
        raise ReadbackError()
    actual_page, size, total, pages = cast(list[int], values)
    if (
        actual_page != page
        or size != PAGE_SIZE
        or total < 0
        or pages < 0
        or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
        and not (total == 0 and pages == 1)
        or page > max(1, pages)
        or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    ):
        raise ReadbackError()
    return ReadPage(tuple(rows), page, pages, total, request_id)


def _compare(expected: Any, actual: Any) -> str:
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
            return "INCOMPLETE"
        results = [_compare(value, actual[key]) for key, value in expected.items()]
        return (
            "INCOMPLETE"
            if "INCOMPLETE" in results
            else "MISMATCH"
            if "MISMATCH" in results
            else "MATCH"
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return "INCOMPLETE"
        if len(expected) != len(actual):
            return "MISMATCH"
        remaining = list(actual)
        for item in expected:
            comparisons = [_compare(item, row) for row in remaining]
            if "MATCH" not in comparisons:
                return "INCOMPLETE" if "INCOMPLETE" in comparisons else "MISMATCH"
            remaining.pop(comparisons.index("MATCH"))
        return "MATCH"
    if type(expected) in {int, float} or isinstance(expected, Decimal):
        if isinstance(actual, bool) or not isinstance(
            actual, (str, int, float, Decimal)
        ):
            return "MISMATCH"
        try:
            left, right = Decimal(str(expected)), Decimal(str(actual))
            return (
                "MATCH"
                if left.is_finite() and right.is_finite() and left == right
                else "MISMATCH"
            )
        except InvalidOperation:
            return "MISMATCH"
    return (
        "MATCH" if type(expected) is type(actual) and expected == actual else "MISMATCH"
    )


def compare_fields(kind: str, expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """Only documented returned fields; missing required evidence never matches."""
    required = {
        "CAMPAIGN": ("advertiser_id", "campaign_name", "budget", "budget_optimize_on"),
        "ADGROUP": ("advertiser_id", "campaign_id", "adgroup_name", "roas_bid"),
        "AD": (
            "advertiser_id",
            "adgroup_id",
            "ad_name",
            "creative_list",
            "ad_text_list",
            "landing_page_url_list",
            "ad_configuration",
        ),
        # This endpoint is advertiser-scoped in the request, not its response.
        "CTA": ("creative_portfolio_type", "portfolio_content"),
    }
    if kind not in required or any(key not in expected for key in required[kind]):
        return "INCOMPLETE"
    optional = {
        "CAMPAIGN": ("budget_mode", "objective_type", "app_promotion_type"),
        "ADGROUP": (
            "optimization_goal",
            "promotion_type",
            "billing_event",
            "bid_type",
            "placement_type",
            "placements",
            "schedule_type",
            "schedule_start_time",
            "schedule_end_time",
            "targeting_spec",
            "minis_id",
        ),
        "AD": ("campaign_id",),
        "CTA": (),
    }
    projection = {
        key: expected[key]
        for key in (*required[kind], *optional[kind])
        if key in expected
    }
    # Budget/ROAS may be canonical decimal strings in frozen JSON.
    for key in ("budget", "roas_bid"):
        if key in projection:
            try:
                projection[key] = Decimal(str(projection[key]))
            except InvalidOperation:
                return "INCOMPLETE"
    return _compare(projection, actual)
