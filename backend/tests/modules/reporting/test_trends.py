from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import select

from app.modules.reporting.models import ReportFact, ReportObservation
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.trends import (
    _drama_series,
    _series_matches_filter,
    build_trend,
)


def test_trend_without_production_observations_is_explicitly_incomplete(report_case):
    report_case.seed_campaign("External Campaign", "report-account", "3", "7")
    trend = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="campaign",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
        grain="day",
    )
    assert trend.points == ()
    assert trend.coverage["status"] == "INCOMPLETE"


def test_trend_consumes_observation_only_when_matching_fact_exists(report_case):
    report_case.seed_campaign("External Campaign", "report-account", "3", "7")
    fact = report_case.session.exec(select(ReportFact)).first()
    assert fact is not None
    observation = ReportObservation(
        tenant_id=report_case.context.tenant_id,
        advertiser_id=fact.advertiser_id,
        subject_kind="campaign",
        subject_key=fact.subject_key,
        bucket_start=fact.bucket_start,
        bucket_end=fact.bucket_end,
        granularity=fact.granularity,
        report_contract=fact.report_contract,
        metric_family=fact.metric_family,
        currency=fact.currency,
        timezone=fact.timezone,
        attribution=fact.attribution,
        values={"spend": "-1.25"},
        availability={"spend": "AVAILABLE"},
        observed_at=datetime(2026, 9, 30, 2, tzinfo=UTC),
        membership_digest="a" * 64,
        published_version=1,
    )
    report_case.session.add(observation)
    report_case.session.flush()
    trend = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="campaign",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
        grain="observation",
    )
    assert trend.coverage["status"] == "COMPLETE"
    assert trend.points[0].values["spend"] == Decimal("-1.25")


def test_trend_keeps_same_bucket_corrections_and_preserves_negative_delta(report_case):
    report_case.seed_campaign("External Campaign", "report-account", "3", "7")
    fact = report_case.session.exec(select(ReportFact)).first()
    assert fact is not None
    common = {
        "tenant_id": report_case.context.tenant_id,
        "advertiser_id": fact.advertiser_id,
        "subject_kind": "campaign",
        "subject_key": fact.subject_key,
        "bucket_start": fact.bucket_start,
        "bucket_end": fact.bucket_end,
        "granularity": fact.granularity,
        "report_contract": fact.report_contract,
        "metric_family": fact.metric_family,
        "currency": fact.currency,
        "timezone": fact.timezone,
        "attribution": fact.attribution,
        "availability": {"spend": "AVAILABLE"},
        "membership_digest": "b" * 64,
        "published_version": 1,
    }
    report_case.session.add_all(
        [
            ReportObservation(
                **common,
                values={"spend": "-1.25"},
                observed_at=datetime(2026, 9, 30, 2, tzinfo=UTC),
            ),
            ReportObservation(
                **common,
                values={"spend": "-2.00"},
                observed_at=datetime(2026, 9, 30, 3, tzinfo=UTC),
            ),
        ]
    )
    report_case.session.flush()
    trend = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="campaign",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
        grain="observation",
    )
    assert len(trend.points) == 2
    assert trend.points[1].delta == {"spend": Decimal("-0.75")}


def test_trend_uses_observation_timezone_for_local_date_window(report_case):
    ref = report_case.seed_campaign("Shanghai Campaign", "report-account", "3", "7")
    start = datetime(2026, 9, 29, 16, tzinfo=UTC)
    fact = ReportFact(
        tenant_id=report_case.context.tenant_id,
        advertiser_id=ref.advertiser_id,
        subject_key=["campaign", ref.remote_id],
        bucket_start=start,
        bucket_end=datetime(2026, 9, 30, 16, tzinfo=UTC),
        granularity="DAY",
        report_contract="basic_campaign",
        metric_family="delivery",
        currency="USD",
        timezone="Asia/Shanghai",
        attribution="default",
        metric_name="spend",
        value=Decimal("5"),
        availability="AVAILABLE",
        published_version=1,
        request_sequence=1,
        source_partition_key="tz-trend",
    )
    report_case.session.add(fact)
    report_case.session.add(
        ReportObservation(
            tenant_id=fact.tenant_id,
            advertiser_id=fact.advertiser_id,
            subject_kind="campaign",
            subject_key=fact.subject_key,
            bucket_start=fact.bucket_start,
            bucket_end=fact.bucket_end,
            granularity="DAY",
            report_contract=fact.report_contract,
            metric_family=fact.metric_family,
            currency=fact.currency,
            timezone=fact.timezone,
            attribution=fact.attribution,
            values={"spend": "5"},
            availability={"spend": "AVAILABLE"},
            observed_at=datetime(2026, 9, 30, 1, tzinfo=UTC),
            membership_digest="c" * 64,
            published_version=1,
        )
    )
    report_case.session.flush()
    trend = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="campaign",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
        grain="day",
    )
    assert len(trend.points) == 1
    assert trend.points[0].values["spend"] == Decimal("5")
    assert trend.points[0].bucket_start == datetime(2026, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_trend_hour_delta_uses_local_date_across_utc_midnight(report_case):
    ref = report_case.seed_campaign("Shanghai Hourly", "report-account", "3", "7")
    for index, start in enumerate(
        (datetime(2026, 9, 29, 23, tzinfo=UTC), datetime(2026, 9, 30, 0, tzinfo=UTC))
    ):
        end = start + timedelta(hours=1)
        fact = ReportFact(
            tenant_id=report_case.context.tenant_id,
            advertiser_id=ref.advertiser_id,
            subject_key=["campaign", ref.remote_id],
            bucket_start=start,
            bucket_end=end,
            granularity="HOUR",
            report_contract="basic_campaign",
            metric_family="delivery",
            currency="USD",
            timezone="Asia/Shanghai",
            attribution="default",
            metric_name="spend",
            value=Decimal(str(index + 1)),
            availability="AVAILABLE",
            published_version=1,
            request_sequence=index + 1,
            source_partition_key=f"tz-hour-{index}",
        )
        report_case.session.add(fact)
        report_case.session.add(
            ReportObservation(
                tenant_id=fact.tenant_id,
                advertiser_id=fact.advertiser_id,
                subject_kind="campaign",
                subject_key=fact.subject_key,
                bucket_start=start,
                bucket_end=end,
                granularity="HOUR",
                report_contract=fact.report_contract,
                metric_family=fact.metric_family,
                currency=fact.currency,
                timezone=fact.timezone,
                attribution=fact.attribution,
                values={"spend": str(index + 1)},
                availability={"spend": "AVAILABLE"},
                observed_at=datetime(2026, 9, 30, 1 + index, tzinfo=UTC),
                membership_digest="e" * 64,
                published_version=index + 1,
            )
        )
    report_case.session.flush()
    trend = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(
            dimension="campaign",
            start_date=report_case.fact_date,
            end_date=report_case.fact_date,
        ),
        grain="hour",
    )
    assert len(trend.points) == 2
    assert trend.points[1].delta == {"spend": Decimal("1")}
    assert trend.points[1].delta_reason is None


def test_trend_applies_shared_keyword_and_spend_filters(report_case):
    ref = report_case.seed_campaign("Included Campaign", "report-account", "3", "7")
    fact = report_case.session.exec(
        select(ReportFact).where(ReportFact.subject_key == ["campaign", ref.remote_id])
    ).first()
    assert fact is not None
    report_case.session.add(
        ReportObservation(
            tenant_id=fact.tenant_id,
            advertiser_id=fact.advertiser_id,
            subject_kind="campaign",
            subject_key=fact.subject_key,
            bucket_start=fact.bucket_start,
            bucket_end=fact.bucket_end,
            granularity=fact.granularity,
            report_contract=fact.report_contract,
            metric_family=fact.metric_family,
            currency=fact.currency,
            timezone=fact.timezone,
            attribution=fact.attribution,
            values={"spend": "3"},
            availability={"spend": "AVAILABLE"},
            observed_at=datetime(2026, 9, 30, 4, tzinfo=UTC),
            membership_digest="d" * 64,
            published_version=1,
        )
    )
    report_case.session.flush()
    base = {
        "dimension": "campaign",
        "start_date": report_case.fact_date,
        "end_date": report_case.fact_date,
    }
    included = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(**base, query="Included", min_spend=Decimal("2")),
        grain="observation",
    )
    excluded = build_trend(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        filters=ReportingFilter(**base, query="Other", min_spend=Decimal("2")),
        grain="observation",
    )
    assert len(included.points) == 1
    assert excluded.points == ()
    assert excluded.coverage["status"] == "INCOMPLETE"


def test_drama_trend_applies_spend_filter_after_member_aggregation():
    from app.modules.ads.models import CampaignNameProjection

    tenant_id = uuid4()
    start = datetime(2026, 9, 30, tzinfo=UTC)
    observations = tuple(
        ReportObservation(
            tenant_id=tenant_id,
            advertiser_id="account-a",
            subject_kind="campaign",
            subject_key=["campaign", campaign_id],
            bucket_start=start,
            bucket_end=start + timedelta(days=1),
            granularity="DAY",
            report_contract="basic_campaign",
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            values={"spend": spend},
            availability={"spend": "AVAILABLE"},
            observed_at=datetime(2026, 9, 30, 1, tzinfo=UTC),
            membership_digest="f" * 64,
            published_version=1,
        )
        for campaign_id, spend in (("campaign-1", "3"), ("campaign-2", "7"))
    )
    projections = tuple(
        CampaignNameProjection(
            tenant_id=tenant_id,
            advertiser_id="account-a",
            campaign_remote_id=campaign_id,
            name_revision=1,
            raw_name="Provider · Drama",
            provider_label="Provider",
            drama_name="Drama",
            status="VALID",
            parser_revision=1,
            grouping_revision=1,
            observed_at=datetime(2026, 9, 30, tzinfo=UTC),
        )
        for campaign_id in ("campaign-1", "campaign-2")
    )
    series = _drama_series(observations, projections, bc_id="bc-test")
    assert len(series) == 1
    assert series[0].values["spend"] == "10"
    assert _series_matches_filter(
        series[0],
        ReportingFilter(
            dimension="drama",
            start_date=start.date(),
            end_date=start.date(),
            min_spend=Decimal("8"),
        ),
    )
