# backend/app/integrations/tiktok/adapters/sdk_report_reader.py
"""官方 SDK 报表请求。每个物理请求都经网关传入的 send（冻结路由 + 额度准入）。"""

import json
from collections.abc import Callable
from datetime import date
from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import (
    RawReportPage,
    page_from_data,
)
from app.modules.reporting.metrics import (
    ACCOUNT_METRICS,
    AD_DAY_METRICS,
    CAMPAIGN_METRICS,
    MATERIAL_METRICS,
)

Send = Callable[[str, str | None, dict[str, Any]], dict]
MAX_ACCOUNTS = 5
MAX_CAMPAIGN_FILTER = 30  # 官方防超时建议：广告级按系列过滤不超过 30 个
_STATUS_ALL = {
    "field_name": "ad_status",
    "filter_type": "IN",
    "filter_value": json.dumps(["STATUS_ALL"]),
}


def _check_range(start: date, end: date, *, max_days: int) -> None:
    if start > end or (end - start).days + 1 > max_days:
        raise DomainError("report_query_invalid", "报表日期段超出平台上限")


def _check_accounts(ids: tuple[str, ...]) -> None:
    if not 1 <= len(ids) <= MAX_ACCOUNTS or len(set(ids)) != len(ids):
        raise DomainError("report_query_invalid", "一次报表请求须带 1–5 个不同账户")


class SdkReportReader:
    def __init__(self, *, send: Send) -> None:
        self._send = send

    def _basic(
        self,
        *,
        advertiser_ids,
        data_level,
        dimensions,
        metrics,
        start,
        end,
        page,
        filtering=None,
        utc=False,
    ) -> RawReportPage:
        _check_accounts(advertiser_ids)
        arguments: dict[str, Any] = {
            "report_type": "BASIC",
            "service_type": "AUCTION",
            "advertiser_ids": list(advertiser_ids),
            "data_level": data_level,
            "dimensions": list(dimensions),
            "metrics": list(metrics),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "page": page,
            "page_size": 1000,
        }
        if filtering:
            arguments["filtering"] = filtering
        if utc:
            arguments["multi_adv_report_in_utc_time"] = True
        data = self._send("reports.integrated", advertiser_ids[0], arguments)
        return page_from_data(data, requested_page=page)

    def bc_spend(self, *, bc_id, start, end, page):
        _check_range(start, end, max_days=365)
        data = self._send(
            "reports.bc",
            None,
            {
                "report_type": "BC",
                "bc_id": bc_id,
                "dimensions": ["advertiser_id"],
                "metrics": ["spend"],
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "page_size": 1000,
            },
        )
        return page_from_data(data, requested_page=page)

    def ad_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
        _check_range(start, end, max_days=30)
        filtering = [_STATUS_ALL]
        if campaign_ids:
            if len(campaign_ids) > MAX_CAMPAIGN_FILTER or len(advertiser_ids) != 1:
                raise DomainError(
                    "report_query_invalid", "按系列过滤只支持单账户且最多 30 个系列"
                )
            filtering.append(
                {
                    "field_name": "campaign_ids",
                    "filter_type": "IN",
                    "filter_value": json.dumps(list(campaign_ids)),
                }
            )
        return self._basic(
            advertiser_ids=advertiser_ids,
            data_level="AUCTION_AD",
            dimensions=("ad_id_v2", "stat_time_day"),
            metrics=AD_DAY_METRICS,
            start=start,
            end=end,
            page=page,
            filtering=filtering,
        )

    def campaign_hour(self, *, advertiser_ids, day, page):
        return self._basic(
            advertiser_ids=advertiser_ids,
            data_level="AUCTION_CAMPAIGN",
            dimensions=("campaign_id", "stat_time_hour"),
            metrics=CAMPAIGN_METRICS,
            start=day,
            end=day,
            page=page,
            filtering=[{**_STATUS_ALL, "field_name": "campaign_status"}],
        )

    def campaign_day_utc(self, *, advertiser_ids, start, end, page):
        _check_range(start, end, max_days=30)
        return self._basic(
            advertiser_ids=advertiser_ids,
            data_level="AUCTION_CAMPAIGN",
            dimensions=("campaign_id", "stat_time_day"),
            metrics=CAMPAIGN_METRICS,
            start=start,
            end=end,
            page=page,
            utc=True,
            filtering=[{**_STATUS_ALL, "field_name": "campaign_status"}],
        )

    def account_day(self, *, advertiser_ids, start, end, page):
        _check_range(start, end, max_days=30)
        return self._basic(
            advertiser_ids=advertiser_ids,
            data_level="AUCTION_ADVERTISER",
            dimensions=("advertiser_id", "stat_time_day"),
            metrics=ACCOUNT_METRICS,
            start=start,
            end=end,
            page=page,
        )

    def material_day(self, *, advertiser_id, start, end, page):
        _check_range(start, end, max_days=30)
        data = self._send(
            "reports.material_breakdown",
            advertiser_id,
            {
                "advertiser_id": advertiser_id,
                "dimensions": ["main_material_id", "stat_time_day"],
                "metrics": list(MATERIAL_METRICS),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "page_size": 100,
            },
        )
        return page_from_data(data, requested_page=page)
