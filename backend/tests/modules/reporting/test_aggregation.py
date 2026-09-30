from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.ads.models import AdMaterialReference, CampaignNameProjection
from app.modules.reporting.aggregation import (
    _build_material_rows,
    aggregate_metrics,
    build_dimension_rows,
)
from app.modules.reporting.models import ReportFact
from app.modules.reporting.schemas import MetricVector, ReportingFilter


def _vector(spend: str, revenue: str, *, state: str = "AVAILABLE") -> MetricVector:
    return MetricVector(
        currency="USD",
        timezone="UTC",
        attribution="default",
        values={
            "spend": Decimal(spend) if state == "AVAILABLE" else None,
            "native_growth_ad_revenue_value_d0": Decimal(revenue) if state == "AVAILABLE" else None,
        },
        availability={
            "spend": state,
            "native_growth_ad_revenue_value_d0": state,
        },
    )


def test_roas_uses_summed_revenue_and_zero_spend_is_undefined():
    result = aggregate_metrics([_vector("10", "30"), _vector("90", "90")])
    assert result.values["d0_roas"] == Decimal("1.2")
    zero = aggregate_metrics([_vector("0", "30")])
    assert zero.values["d0_roas"] is None


def test_missing_and_unsupported_are_not_converted_to_zero():
    with pytest.raises(ValueError, match="availability buckets"):
        aggregate_metrics([_vector("10", "30"), _vector("0", "0", state="MISSING")])
    missing = aggregate_metrics([_vector("0", "0", state="MISSING")])
    assert missing.values["spend"] is None
    assert missing.availability["spend"] == "MISSING"
    unsupported = aggregate_metrics(
        [
            MetricVector(
                currency="USD",
                timezone="UTC",
                attribution="default",
                values={"spend": None},
                availability={"spend": "UNSUPPORTED"},
            )
        ]
    )
    assert unsupported.values["spend"] is None
    assert unsupported.availability["spend"] == "UNSUPPORTED"


def test_material_usage_not_double_counted():
    tenant_id = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    materials = tuple(
        AdMaterialReference(
            tenant_id=tenant_id,
            advertiser_id="account-a",
            ad_remote_id=ad_id,
            platform_material_id="vid-shared",
            material_type="VIDEO",
            complete=True,
            published_version=1,
        )
        for ad_id in ("ad-3", "ad-7")
    )
    facts = tuple(
        ReportFact(
            tenant_id=tenant_id,
            advertiser_id="account-a",
            subject_key=["material", "campaign_id", "campaign", "vid-shared", "VIDEO"],
            bucket_start=start,
            bucket_end=start + timedelta(days=1),
            granularity="RANGE",
            report_contract="material_overview",
            metric_family="material",
            currency="USD",
            timezone="UTC",
            attribution="default",
            metric_name="spend",
            value=Decimal(amount),
            availability="AVAILABLE",
            attributes={"ad_id": ad_id},
            published_version=1,
            request_sequence=1,
            source_partition_key="material-test",
        )
        for ad_id, amount in (("ad-3", "3"), ("ad-7", "7"))
    )
    rows = _build_material_rows(
        facts,
        materials,
        ReportingFilter(dimension="material", start_date=start.date(), end_date=start.date()),
    )
    assert rows[0].metric_buckets[0].values["spend"] == Decimal("10")
    assert rows[0].metric_buckets[0].values["native_growth_ad_revenue_value_d0"] is None
    assert rows[0].metric_buckets[0].availability["native_growth_ad_revenue_value_d0"] == "UNSUPPORTED"
    assert {item.ad_ref.remote_id for item in rows[0].material_uses} == {"ad-3", "ad-7"}


def test_mixed_currency_or_timezone_is_rejected():
    with pytest.raises(ValueError, match="incompatible"):
        aggregate_metrics(
            [
                _vector("1", "1"),
                MetricVector(
                    currency="EUR",
                    timezone="UTC",
                    attribution="default",
                    values={"spend": Decimal("1"), "native_growth_ad_revenue_value_d0": Decimal("1")},
                    availability={"spend": "AVAILABLE", "native_growth_ad_revenue_value_d0": "AVAILABLE"},
                ),
            ]
        )


def test_dimension_rows_keep_external_campaign_visible(report_case):
    report_case.seed_campaign("External Campaign", "report-account", "3", "7")
    rows = build_dimension_rows(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="drama",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
    )
    assert rows
    assert all(row.row_key.startswith("bc-report-case:") for row in rows)
    assert any(":external:" in row.row_key for row in rows)


def test_dimension_rows_group_valid_campaign_by_provider_and_drama(report_case):
    ref = report_case.seed_campaign("Provider · My Drama · note", "report-account", "3", "7")
    report_case.session.add(
        CampaignNameProjection(
            tenant_id=ref.tenant_id,
            advertiser_id=ref.advertiser_id,
            campaign_remote_id=ref.remote_id,
            name_revision=1,
            raw_name="Provider · My Drama · note",
            provider_label="Provider",
            drama_name="My Drama",
            status="VALID",
            parser_revision=1,
            grouping_revision=1,
            observed_at=datetime.now(UTC),
        )
    )
    report_case.session.flush()
    rows = build_dimension_rows(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="drama",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
    )
    assert [row.row_key for row in rows] == ["bc-report-case:drama:Provider:My Drama"]
    assert rows[0].refs[0].remote_id == ref.remote_id
