from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.reporting import ReportPage, ReportRow
from app.modules.reporting.contracts import (
    METRIC_DEFINITIONS,
    REPORT_CONTRACTS,
    decode_query,
    report_partition_key,
    supports_metric,
    validate_query,
)
from app.modules.reporting.facts import (
    observation_delta,
    publish_report,
    stage_report_page,
)
from app.modules.reporting.models import ReportCoverage, ReportFact
from app.modules.reporting.sync_models import ReportSyncRun


def _run(session, reporting_seed, *, metrics=("spend", "clicks")):
    context = reporting_seed.context
    connection_id = reporting_seed.connection.id
    route = {
        "tenant_id": str(context.tenant_id),
        "bc_id": "bc-report",
        "connection_id": str(connection_id),
        "channel": "OFFICIAL_API",
        "authorization_revision": 0,
        "binding_revision": 0,
        "adapter_contract_revision": "official-api-v1",
    }
    run = ReportSyncRun(
        tenant_id=context.tenant_id,
        advertiser_id="report-account",
        bc_id="bc-report",
        actor_id=context.actor_id,
        connection_id=connection_id,
        frozen_route=route,
        partition_key=report_partition_key(decode_query({
            "advertiser_id": "report-account",
            "report_contract": "basic_campaign",
            "metric_family": "delivery",
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": list(metrics),
            "start_date": "2026-09-01",
            "end_date": "2026-09-01",
            "granularity": "DAY",
            "currency": "USD",
            "timezone": "UTC",
            "attribution": "default",
            "filter_ids": ["campaign-1"],
        })),
        query={
            "advertiser_id": "report-account",
            "report_contract": "basic_campaign",
            "metric_family": "delivery",
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": list(metrics),
            "start_date": "2026-09-01",
            "end_date": "2026-09-01",
            "granularity": "DAY",
            "currency": "USD",
            "timezone": "UTC",
            "attribution": "default",
            "filter_ids": ["campaign-1"],
        },
        claim_generation=1,
    )
    session.add(run)
    session.flush()
    return run


def _page(*, values, complete=True):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return ReportPage(
        rows=(
            ReportRow(
                subject_key=("campaign", "campaign-1"),
                bucket_start=start,
                bucket_end=start + timedelta(days=1),
                values=values,
                availability=dict.fromkeys(values, "AVAILABLE"),
            ),
        ),
        next_page=None,
        complete=complete,
        evidence=CallEvidence(request_id="offline-test"),
    )


def _page_at(start, *, values):
    return ReportPage(
        rows=(
            ReportRow(
                subject_key=("campaign", "campaign-1"),
                bucket_start=start,
                bucket_end=start + timedelta(days=1),
                values=values,
                availability=dict.fromkeys(values, "AVAILABLE"),
            ),
        ),
        next_page=None,
        complete=True,
        evidence=CallEvidence(request_id=f"offline-{start.date()}"),
    )


def test_metric_contracts_keep_native_growth_out_of_material_reports():
    assert {"spend", "impressions", "clicks"} <= set(METRIC_DEFINITIONS)
    assert supports_metric(
        report_contract="basic_campaign",
        metric_name="native_growth_ad_revenue_value_d0",
    )
    assert not supports_metric(
        report_contract="material_overview",
        metric_name="native_growth_ad_revenue_value_d0",
    )
    assert "main_material_id" in REPORT_CONTRACTS["material_overview"].dimensions


def test_restricted_contracts_require_ad_type_and_reject_basic_alias():
    query = {
        "advertiser_id": "report-account",
        "report_contract": "basic_ad",
        "metric_family": "delivery",
        "dimensions": ["ad_id", "stat_time_day"],
        "metrics": ["spend"],
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "granularity": "DAY",
        "currency": "USD",
        "timezone": "UTC",
        "attribution": "default",
        "filter_ids": [],
    }
    with pytest.raises(ValueError):
        validate_query(decode_query(query), channel="OFFICIAL_API")
    assert validate_query(
        decode_query(query | {"ad_type": "REGULAR"}),
        channel="OFFICIAL_API",
        ad_type="REGULAR",
    ).key == "basic_ad"
    decoded = decode_query(query)
    assert report_partition_key(decoded, ad_type="REGULAR") != report_partition_key(
        decoded, ad_type="LEGACY_SMART_PLUS"
    )
    with pytest.raises(ValueError):
        validate_query(
            decode_query(query | {"report_contract": "basic"}),
            channel="OFFICIAL_API",
            ad_type="REGULAR",
        )


def test_observation_delta_preserves_negative_corrections_and_fences_context(
    observations,
):
    assert observation_delta(observations.previous, observations.corrected)["spend"] == Decimal(
        "-1.25"
    )
    assert observation_delta(observations.previous, observations.renamed) == {
        "spend": Decimal("-1.25"),
        "impressions": Decimal("0"),
    }
    assert observation_delta(observations.previous, observations.next_day) is None


def test_publish_replaces_complete_partition_without_erasing_other_metric_group(
    session, reporting_seed, monkeypatch
):
    # 授权门禁在独立路由测试中验证；本测试只使用真实 DB 验证发布事务和分片隔离。
    monkeypatch.setattr("app.modules.reporting.facts._ensure_route_authority", lambda *args: None)
    first = _run(session, reporting_seed)
    stage_report_page(
        session,
        run_id=first.id,
        page=_page(values={"spend": Decimal("10.00"), "clicks": Decimal("2")}),
        claim_generation=1,
    )
    version = publish_report(session, run_id=first.id, claim_generation=1)
    assert version > 0
    first_facts = session.exec(select(ReportFact)).all()
    assert {row.metric_name for row in first_facts} == {"spend", "clicks"}
    assert next(row for row in first_facts if row.metric_name == "spend").value == Decimal("10.00")

    other = _run(session, reporting_seed, metrics=("impressions",))
    stage_report_page(
        session,
        run_id=other.id,
        page=_page(values={"impressions": Decimal("100")}),
        claim_generation=1,
    )
    publish_report(session, run_id=other.id, claim_generation=1)
    assert {row.metric_name for row in session.exec(select(ReportFact)).all()} == {
        "spend",
        "clicks",
        "impressions",
    }

    # 同一完整分片的空结果只替换该分片，且不会把缺失指标填成零。
    empty = _run(session, reporting_seed)
    stage_report_page(
        session,
        run_id=empty.id,
        page=ReportPage(
            rows=(),
            next_page=None,
            complete=True,
            evidence=CallEvidence(request_id="offline-empty"),
        ),
        claim_generation=1,
    )
    publish_report(session, run_id=empty.id, claim_generation=1)
    remaining = session.exec(select(ReportFact)).all()
    assert {row.metric_name for row in remaining} == {"impressions"}
    coverage = session.exec(
        select(ReportCoverage).where(ReportCoverage.partition_key == first.partition_key)
    ).one()
    assert coverage.status == "COMPLETE_EMPTY"


def test_missing_metric_is_not_filled_with_zero_and_real_zero_is_preserved(
    session, reporting_seed, monkeypatch
):
    monkeypatch.setattr("app.modules.reporting.facts._ensure_route_authority", lambda *args: None)
    run = _run(session, reporting_seed)
    stage_report_page(
        session,
        run_id=run.id,
        page=_page(values={"spend": Decimal("0")}),
        claim_generation=1,
    )
    with pytest.raises(DomainError):
        publish_report(session, run_id=run.id, claim_generation=1)

    complete = _run(session, reporting_seed, metrics=("spend", "clicks"))
    stage_report_page(
        session,
        run_id=complete.id,
        page=_page(values={"spend": Decimal("0"), "clicks": Decimal("0")}),
        claim_generation=1,
    )
    publish_report(session, run_id=complete.id, claim_generation=1)
    facts = session.exec(select(ReportFact)).all()
    assert {row.metric_name: row.value for row in facts} == {
        "spend": Decimal("0"),
        "clicks": Decimal("0"),
    }


def test_unknown_only_rows_are_rejected_without_replacing_old_facts(
    session, reporting_seed, monkeypatch
):
    monkeypatch.setattr("app.modules.reporting.facts._ensure_route_authority", lambda *args: None)
    original = _run(session, reporting_seed, metrics=("spend",))
    stage_report_page(
        session,
        run_id=original.id,
        page=_page(values={"spend": Decimal("7")}),
        claim_generation=1,
    )
    publish_report(session, run_id=original.id, claim_generation=1)

    invalid = _run(session, reporting_seed, metrics=("spend",))
    with pytest.raises(DomainError) as error:
        stage_report_page(
            session,
            run_id=invalid.id,
            page=_page(values={"unsupported_metric": Decimal("999")}),
            claim_generation=1,
        )
    assert error.value.code == "report_metrics_invalid"
    assert session.exec(select(ReportFact)).one().value == Decimal("7")


def test_old_partition_run_cannot_overwrite_newer_run(session, reporting_seed, monkeypatch):
    monkeypatch.setattr("app.modules.reporting.facts._ensure_route_authority", lambda *args: None)
    old = _run(session, reporting_seed, metrics=("spend",))
    new = _run(session, reporting_seed, metrics=("spend",))
    for run, amount in ((new, "20"), (old, "10")):
        stage_report_page(
            session,
            run_id=run.id,
            page=_page(values={"spend": Decimal(amount)}),
            claim_generation=1,
        )
    publish_report(session, run_id=new.id, claim_generation=1)
    with pytest.raises(DomainError):
        publish_report(session, run_id=old.id, claim_generation=1)
    assert session.exec(select(ReportFact)).one().value == Decimal("20")


def test_observation_currency_timezone_attribution_membership_and_group_changes(
    observations,
):
    for field, value in (
        ("currency", "EUR"),
        ("timezone", "Asia/Shanghai"),
        ("attribution", "different"),
        ("membership_digest", "b" * 64),
        ("grouping_revision", 2),
    ):
        current = observations.corrected.model_copy(update={field: value})
        assert observation_delta(observations.previous, current) is None


def test_complete_empty_replaces_every_requested_local_day_bucket(
    session, reporting_seed, monkeypatch
):
    monkeypatch.setattr(
        "app.modules.reporting.facts._ensure_route_authority", lambda *args: None
    )
    first = _run(session, reporting_seed)
    first.query = first.query | {"end_date": "2026-09-02"}
    first.partition_key = report_partition_key(decode_query(first.query))
    session.flush()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    stage_report_page(
        session,
        run_id=first.id,
        page=_page_at(start, values={"spend": Decimal("1"), "clicks": Decimal("1")}),
        claim_generation=1,
    )
    # Two pages are represented by a complete first page in this fixture; publish
    # an explicit second run below to seed the second bucket before clearing it.
    second = _run(session, reporting_seed)
    second.query = second.query | {"end_date": "2026-09-02"}
    second.partition_key = report_partition_key(decode_query(second.query))
    session.flush()
    stage_report_page(
        session,
        run_id=second.id,
        page=_page_at(start + timedelta(days=1), values={"spend": Decimal("2"), "clicks": Decimal("2")}),
        claim_generation=1,
    )
    # The first page run is intentionally not published; the second run proves
    # the target bucket calculation and then a later empty run clears all buckets.
    publish_report(session, run_id=second.id, claim_generation=1)
    empty = _run(session, reporting_seed)
    empty.query = empty.query | {"end_date": "2026-09-02"}
    empty.partition_key = report_partition_key(decode_query(empty.query))
    session.flush()
    stage_report_page(
        session,
        run_id=empty.id,
        page=ReportPage(rows=(), next_page=None, complete=True, evidence=CallEvidence()),
        claim_generation=1,
    )
    publish_report(session, run_id=empty.id, claim_generation=1)
    assert session.exec(select(ReportFact)).all() == []
    coverages = session.exec(select(ReportCoverage)).all()
    assert len(coverages) == 2
    assert {item.status for item in coverages} == {"COMPLETE_EMPTY"}


def test_staged_attributes_are_allowlisted_and_urls_are_dropped(
    session, reporting_seed, monkeypatch
):
    monkeypatch.setattr(
        "app.modules.reporting.facts._ensure_route_authority", lambda *args: None
    )
    run = _run(session, reporting_seed, metrics=("spend",))
    start = datetime(2026, 9, 1, tzinfo=UTC)
    page = ReportPage(
        rows=(
            ReportRow(
                subject_key=("campaign", "campaign-1"),
                bucket_start=start,
                bucket_end=start + timedelta(days=1),
                values={"spend": Decimal("1")},
                availability={"spend": "AVAILABLE"},
                attributes={
                    "campaign_name": "版权方-剧名-备注",
                    "download_url": " HTTPS://signed.example/file?token=x ",
                    "raw_response": {"secret": "payload"},
                },
            ),
        ),
        next_page=None,
        complete=True,
        evidence=CallEvidence(request_id="attributes"),
    )
    stage_report_page(session, run_id=run.id, page=page, claim_generation=1)
    publish_report(session, run_id=run.id, claim_generation=1)
    fact = session.exec(select(ReportFact)).one()
    assert fact.attributes == {"campaign_name": "版权方-剧名-备注"}


def test_nested_allowlisted_attributes_are_rejected(session, reporting_seed, monkeypatch):
    monkeypatch.setattr(
        "app.modules.reporting.facts._ensure_route_authority", lambda *args: None
    )
    run = _run(session, reporting_seed, metrics=("spend",))
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(DomainError) as error:
        stage_report_page(
            session,
            run_id=run.id,
            page=ReportPage(
                rows=(
                    ReportRow(
                        subject_key=("campaign", "campaign-1"),
                        bucket_start=start,
                        bucket_end=start + timedelta(days=1),
                        values={"spend": Decimal("1")},
                        availability={"spend": "AVAILABLE"},
                        attributes={"campaign_name": {"raw": "payload"}},
                    ),
                ),
                next_page=None,
                complete=True,
                evidence=CallEvidence(request_id="nested-attributes"),
            ),
            claim_generation=1,
        )
    assert error.value.code == "report_attributes_invalid"
