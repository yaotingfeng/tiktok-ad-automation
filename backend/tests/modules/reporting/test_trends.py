from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import select

from app.modules.reporting.models import ReportFact, ReportObservation
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.trends import build_trend


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
