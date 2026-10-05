from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import business_api_client as sdk
import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters import sdk_reporting
from app.integrations.tiktok.adapters.sdk_reporting import (
    SdkReportingOperations,
    _async_payload,
    _page,
    _payload,
    plan_report_shards,
)
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


def test_async_payload_matches_generated_task_model(report_query):
    payload = _async_payload(report_query)
    assert "page" not in payload
    assert "page_size" not in payload
    assert payload["enable_report_title_translation"] is False
    sdk.ReportTaskCreateBody(**payload)
    with pytest.raises(TypeError):
        sdk.ReportTaskCreateBody(**_payload(report_query))


@pytest.mark.parametrize(
    ("report_contract", "identity"),
    [
        ("basic_account", "advertiser_id"),
        ("basic_campaign", "campaign_id"),
        ("basic_adgroup", "adgroup_id"),
        ("basic_ad", "ad_id"),
        ("basic_smart_plus_ad", "ad_id_v2"),
        ("basic_smart_plus_creative", "ad_id"),
    ],
)
def test_async_typed_parent_fails_closed_without_verified_type_filter(
    report_query, report_contract, identity
):
    query = replace(
        report_query,
        report_contract=report_contract,
        dimensions=(identity, "stat_time_day"),
    )
    with pytest.raises(DomainError) as failure:
        _async_payload(query, ad_type="SMART_PLUS")
    assert failure.value.code == "report_async_ad_type_unsupported"


def test_ad_type_is_a_real_outbound_filter():
    query = ReportQuery(
        advertiser_id="report-account",
        report_contract="basic_ad",
        metric_family="delivery",
        dimensions=("ad_id", "stat_time_day"),
        metrics=("spend",),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        granularity="DAY",
        currency="USD",
        timezone="UTC",
        attribution="default",
        filter_ids=("ad-1",),
        page=1,
    )
    regular = _payload(query, ad_type="REGULAR")
    legacy = _payload(query, ad_type="LEGACY_SMART_PLUS")
    assert regular["filtering"] != legacy["filtering"]


def test_smart_plus_creative_uses_parent_outbound_filter():
    query = ReportQuery(
        advertiser_id="report-account",
        report_contract="basic_smart_plus_creative",
        metric_family="delivery",
        dimensions=("ad_id", "stat_time_day"),
        metrics=("spend",),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        granularity="DAY",
        currency="USD",
        timezone="UTC",
        attribution="default",
        filter_ids=(),
        page=1,
    )
    assert _payload(query, ad_type="SMART_PLUS")["filtering"][-1] == {
        "field_name": "campaign_automation_type",
        "filter_type": "IN",
        "filter_value": '["UPGRADED_SMART_PLUS"]',
    }


def test_material_day_accepts_date_only_and_rejects_short_totals():
    query = ReportQuery(
        advertiser_id="report-account",
        report_contract="material_breakdown",
        metric_family="material",
        dimensions=("main_material_id", "stat_time_day"),
        metrics=("spend",),
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 24),
        granularity="DAY",
        currency="USD",
        timezone="UTC",
        attribution="default",
        filter_ids=(),
        page=1,
    )
    response = {
        "data": {
            "list": [
                {
                    "dimensions": {
                        "main_material_id": "m-1",
                        "main_material_type": "VIDEO_NON_SPARK_ADS",
                        "stat_time_day": "2026-09-24",
                    },
                    "metrics": {"spend": "1.00"},
                }
            ],
            "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 2},
        },
        "request_id": "offline",
    }
    with pytest.raises(DomainError):
        _page(query, response, set())
    response["data"]["page_info"]["total_number"] = 1
    page = _page(query, response, set())
    assert page.rows[0].subject_key == (
        "material",
        "main_material_id",
        "m-1",
        "m-1",
        "VIDEO_NON_SPARK_ADS",
    )


def test_malformed_row_is_bounded_domain_error(report_query):
    response = {
        "data": {
            "list": [None],
            "page_info": {"page": 1, "page_size": 1, "total_page": 1, "total_number": 1},
        },
        "request_id": "offline",
    }
    with pytest.raises(DomainError):
        _page(report_query, response, set())


@pytest.mark.parametrize(
    "page_info",
    [
        {"page": 2, "page_size": 1000, "total_page": 1, "total_number": 1},
        {"page": 1, "page_size": 1000, "total_page": 2, "total_number": 1},
    ],
)
def test_page_rejects_out_of_range_or_inconsistent_totals(report_query, page_info):
    query = replace(report_query, page=page_info["page"])
    response = {
        "data": {"list": [], "page_info": page_info},
        "request_id": "offline",
    }
    seen = set()
    with pytest.raises(DomainError) as failure:
        _page(query, response, seen)
    assert failure.value.code == "report_response_invalid"
    assert seen == set()


def test_expired_download_never_opens_signed_url(monkeypatch):
    opened = False

    def forbidden_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("expired download must not open the URL")

    monkeypatch.setattr(sdk_reporting.urllib.request, "urlopen", forbidden_open)
    with pytest.raises(DomainError) as failure:
        SdkReportingOperations._fetch_file(
            "https://example.invalid/report.csv",
            datetime.now(UTC) - timedelta(seconds=1),
        )
    assert failure.value.code == "read_deadline_exceeded"
    assert opened is False


def test_download_stream_checks_deadline_after_each_read(monkeypatch):
    base = datetime(2026, 9, 30, tzinfo=UTC)
    deadline = base + timedelta(seconds=1)

    class FakeClock:
        _times = iter((base, base, base + timedelta(seconds=2)))

        @classmethod
        def now(cls, _tz):
            return next(cls._times)

    class FakeResponse:
        headers = {"Content-Length": "3"}

        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            self.reads += 1
            return b"abc"

    response = FakeResponse()
    monkeypatch.setattr(sdk_reporting, "datetime", FakeClock)
    monkeypatch.setattr(sdk_reporting.urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    with pytest.raises(DomainError) as failure:
        SdkReportingOperations._fetch_file("https://example.invalid/report.csv", deadline)
    assert failure.value.code == "read_deadline_exceeded"
    assert response.reads == 1
