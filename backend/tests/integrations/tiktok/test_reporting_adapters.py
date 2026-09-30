from datetime import date

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_reporting import _page, plan_report_shards
from app.integrations.tiktok.contracts.reporting import ReportQuery


@pytest.fixture
def report_query():
    return ReportQuery(
        advertiser_id="report-account",
        report_contract="basic_campaign",
        metric_family="delivery",
        dimensions=("campaign_id", "stat_time_day"),
        metrics=("spend", "clicks"),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        granularity="DAY",
        currency="USD",
        timezone="UTC",
        attribution="default",
        filter_ids=("1",),
        page=1,
    )


def test_plan_report_shards_respects_platform_id_cap(report_query):
    shards = plan_report_shards(
        report_query, entity_ids=tuple(str(i) for i in range(20_001)), max_ids=100
    )
    assert len(shards) == 201
    assert sum(len(shard.filter_ids) for shard in shards) == 20_001
    assert all(shard.page == 1 for shard in shards)


def test_sync_page_preserves_missing_metric_and_rejects_repeat(report_query):
    response = {
        "data": {
            "list": [
                {
                    "dimensions": {"campaign_id": "1", "stat_time_day": "2026-09-01 00:00:00"},
                    "metrics": {"spend": "0.00", "clicks": "-"},
                }
            ],
            "page_info": {"page": 1, "page_size": 1000, "total_page": 1, "total_number": 1},
        },
        "request_id": "offline",
    }
    seen = set()
    page = _page(report_query, response, seen)
    assert page.rows[0].values["spend"] == 0
    assert page.rows[0].availability["clicks"] == "MISSING"
    with pytest.raises(DomainError):
        _page(report_query, response, seen)
