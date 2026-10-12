# backend/tests/modules/reporting/test_metrics.py
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.modules.reporting.metrics import (
    AD_DAY_METRICS,
    basic_metric_values,
    material_metric_values,
    parse_decimal,
    parse_local_day,
    parse_local_hour,
)


def test_dash_is_null_not_zero():
    assert parse_decimal("-") is None and parse_decimal(None) is None
    assert parse_decimal("0.00") == Decimal("0.00")


def test_rejects_non_finite_and_float():
    for bad in ("NaN", "Infinity", 1.5, "abc"):
        with pytest.raises(DomainError):
            parse_decimal(bad)


def test_basic_mapping_uses_platform_day_offsets():
    values = basic_metric_values(
        {
            "spend": "10",
            "impressions": "100",
            "clicks": "5",
            "native_growth_total_ad_impression_event_count": "3",
            "native_growth_ad_revenue_value_d0": "1",
            "native_growth_ad_revenue_value_d2": "2",
            "native_growth_ad_revenue_value_d6": "3",
            "native_growth_ad_revenue_value_d13": "4",
            "native_growth_ad_revenue_value_d29": "5",
            "native_growth_total_ad_impression_value": "-",
        }
    )
    assert values["revenue_d3"] == Decimal("2") and values["revenue_d30"] == Decimal(
        "5"
    )
    assert values["revenue_total"] is None
    assert (
        "campaign_id" in AD_DAY_METRICS
        and "native_growth_ad_revenue_value_d29" in AD_DAY_METRICS
    )


def test_material_has_no_d3_d30():
    values = material_metric_values(
        {
            "spend": "1",
            "impressions": "2",
            "clicks": "1",
            "onsite_total_ad_impression": "1",
            "onsite_ad_impression_ad_revenue_day0": "0.5",
            "onsite_ad_impression_ad_revenue_day6": "0.6",
            "onsite_ad_impression_ad_revenue_day13": "0.7",
            "onsite_total_ad_impression_value": "0.8",
        }
    )
    assert values["revenue_d3"] is None and values["revenue_d30"] is None
    assert values["revenue_d7"] == Decimal("0.6")


def test_day_and_hour_parsing():
    assert parse_local_day("2026-10-05 00:00:00") == date(2026, 10, 5)
    assert parse_local_day("2026-10-05") == date(2026, 10, 5)
    # 香港 10:00 = UTC 02:00
    assert parse_local_hour("2026-10-05 10:00:00", "Asia/Hong_Kong") == datetime(
        2026, 10, 5, 2, tzinfo=UTC
    )


def test_missing_hour_dimension_is_a_report_error():
    import pytest

    from app.core.errors import DomainError
    from app.modules.reporting.metrics import parse_local_hour

    with pytest.raises(DomainError, match="报表小时维度无效"):
        parse_local_hour(None, "UTC")
