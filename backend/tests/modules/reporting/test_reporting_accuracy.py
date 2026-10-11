"""广告报表数据准确性回归：类型隔离、口径合并、排序、调度与重试。"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_reporting import _bucket
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.reporting import ReportQuery
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.ads.models import AdObject
from app.modules.reporting.aggregation import _sort_rows, build_dimension_rows
from app.modules.reporting.models import ReportCoverage, ReportFact
from app.modules.reporting.scheduling import enqueue_due_syncs, ensure_sync_schedules
from app.modules.reporting.schemas import MetricVector, ReportingFilter, ReportRow
from app.modules.reporting.sync import MAX_PROVIDER_RETRIES, collect_report_step
from app.modules.reporting.sync_models import ReportSyncRun, SyncSchedule

DAY = date(2026, 9, 30)
START = datetime.combine(DAY, time.min, tzinfo=UTC)
METRICS = ("spend", "native_growth_ad_revenue_value_d0")


def _route_json(case) -> dict:
    run = case.session.exec(
        select(ReportSyncRun).where(ReportSyncRun.tenant_id == case.context.tenant_id)
    ).first()
    assert run is not None, "report_case 先通过 seed_campaign 建立冻结路由"
    return run


def _run(case, *, contract: str, ad_type: str | None) -> ReportSyncRun:
    template = _route_json(case)
    query = {"report_contract": contract}
    if ad_type is not None:
        query["ad_type"] = ad_type
    run = ReportSyncRun(
        tenant_id=case.context.tenant_id,
        advertiser_id="report-account",
        bc_id=case.bc_id,
        actor_id=case.context.actor_id,
        connection_id=template.connection_id,
        channel="OFFICIAL_API",
        frozen_route=template.frozen_route,
        request_id=uuid4(),
        partition_key=sha256(uuid4().bytes).hexdigest(),
        query=query,
        status="COMPLETE",
        coverage="COMPLETE",
        next_page=1,
    )
    case.session.add(run)
    case.session.flush()
    return run


def _fact(case, run, *, contract, subject, metric, value, day=DAY, sequence=1, state="AVAILABLE"):
    start = datetime.combine(day, time.min, tzinfo=UTC)
    case.session.add(
        ReportFact(
            tenant_id=case.context.tenant_id,
            advertiser_id="report-account",
            subject_key=subject,
            bucket_start=start,
            bucket_end=start + timedelta(days=1),
            granularity="DAY",
            report_contract=contract,
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            metric_name=metric,
            value=None if value is None else Decimal(value),
            availability=state,
            published_version=sequence,
            request_sequence=sequence,
            source_partition_key=run.partition_key,
            source_run_id=run.id,
        )
    )


def _empty_coverage(case, run, *, contract, sequence):
    case.session.add(
        ReportCoverage(
            tenant_id=case.context.tenant_id,
            advertiser_id="report-account",
            bucket_start=START,
            bucket_end=START + timedelta(days=1),
            granularity="DAY",
            report_contract=contract,
            metric_family="delivery",
            currency="USD",
            timezone="UTC",
            attribution="default",
            partition_key=run.partition_key,
            filter_ids=[],
            requested_metrics=list(METRICS),
            dimensions=["ad_id", "stat_time_day"],
            status="COMPLETE_EMPTY",
            observed_at=datetime.now(UTC),
            published_version=sequence,
            request_sequence=sequence,
            source_run_id=run.id,
        )
    )


def _filters(dimension: str, **extra) -> ReportingFilter:
    return ReportingFilter(dimension=dimension, start_date=DAY, end_date=DAY, **extra)


def _spend(row) -> Decimal | None:
    assert len(row.metric_buckets) == 1
    return row.metric_buckets[0].values["spend"]


def test_legacy_empty_result_does_not_hide_regular_ad_spend(report_case):
    """旧版 Smart+ 分区的完整空结果不能屏蔽同账户普通广告的消耗。"""
    report_case.seed_campaign("Provider-Drama", "report-account", 1, 1)
    regular = _run(report_case, contract="basic_ad", ad_type="REGULAR")
    for metric, value in (("spend", "5"), ("native_growth_ad_revenue_value_d0", "2")):
        _fact(report_case, regular, contract="basic_ad", subject=["ad", "ad-r"], metric=metric, value=value)
    legacy = _run(report_case, contract="basic_ad", ad_type="LEGACY_SMART_PLUS")
    _empty_coverage(report_case, legacy, contract="basic_ad", sequence=2)
    report_case.session.flush()

    rows = build_dimension_rows(
        report_case.session, context=report_case.context, bc_id=report_case.bc_id,
        filters=_filters("ad"),
    )
    ad_rows = [row for row in rows if row.display.get("remote_id") == "ad-r"]
    assert len(ad_rows) == 1
    assert _spend(ad_rows[0]) == Decimal("5")

    # 同类型的较新完整空结果仍然代表平台修正为“无数据”，必须继续屏蔽旧值。
    newer_regular = _run(report_case, contract="basic_ad", ad_type="REGULAR")
    _empty_coverage(report_case, newer_regular, contract="basic_ad", sequence=3)
    report_case.session.flush()
    rows = build_dimension_rows(
        report_case.session, context=report_case.context, bc_id=report_case.bc_id,
        filters=_filters("ad"),
    )
    assert not [row for row in rows if row.display.get("remote_id") == "ad-r"]


def test_partial_metric_day_keeps_one_bucket_and_never_undercounts(report_case):
    """某天收入缺失时整行仍是一个口径：消耗照常合计，收入为空而不是只算部分天。"""
    ref = report_case.seed_campaign("Provider-Drama", "report-account", "10", "4")
    run = _run(report_case, contract="basic_campaign", ad_type=None)
    next_day = DAY + timedelta(days=1)
    _fact(report_case, run, contract="basic_campaign", subject=["campaign", ref.remote_id],
          metric="spend", value="6", day=next_day)
    _fact(report_case, run, contract="basic_campaign", subject=["campaign", ref.remote_id],
          metric="native_growth_ad_revenue_value_d0", value=None, day=next_day, state="MISSING")
    report_case.session.flush()

    rows = build_dimension_rows(
        report_case.session, context=report_case.context, bc_id=report_case.bc_id,
        filters=ReportingFilter(dimension="campaign", start_date=DAY, end_date=next_day),
    )
    row = next(item for item in rows if item.display.get("remote_id") == ref.remote_id)
    assert len(row.metric_buckets) == 1
    vector = row.metric_buckets[0]
    assert vector.values["spend"] == Decimal("16")
    assert vector.values["native_growth_ad_revenue_value_d0"] is None
    assert vector.availability["native_growth_ad_revenue_value_d0"] == "MISSING"
    assert vector.values["d0_roas"] is None


def _row(key: str, *, spend: str | None, roas: str | None, currency="USD", name=None, campaign=None):
    values = {"spend": None if spend is None else Decimal(spend),
              "d0_roas": None if roas is None else Decimal(roas)}
    vector = MetricVector(
        currency=currency, timezone="UTC", attribution="default", values=values,
        availability={k: ("AVAILABLE" if v is not None else "MISSING") for k, v in values.items()},
    )
    return ReportRow(row_key=key, display={"name": name or key, "campaign_name": campaign},
                     metric_buckets=(vector,))


def test_every_metric_column_sorts_with_missing_last():
    rows = [
        _row("a", spend="5", roas="0.5"),
        _row("b", spend=None, roas=None),
        _row("c", spend="20", roas="1.5"),
        _row("d", spend="10", roas="0.9"),
    ]
    desc = _sort_rows(rows, _filters("campaign", sort_by="d0_roas", sort_direction="desc"))
    assert [row.row_key for row in desc] == ["c", "d", "a", "b"]
    asc = _sort_rows(rows, _filters("campaign", sort_by="spend", sort_direction="asc"))
    assert [row.row_key for row in asc] == ["a", "d", "c", "b"]


def test_mixed_currency_spend_sort_groups_instead_of_failing():
    rows = [_row("a", spend="5", roas=None, currency="USD"),
            _row("b", spend="9", roas=None, currency="EUR"),
            _row("c", spend="7", roas=None, currency="USD")]
    ordered = _sort_rows(rows, _filters("campaign", sort_by="spend", sort_direction="desc"))
    assert [row.row_key for row in ordered] == ["c", "a", "b"]


def test_parent_name_column_sorts_by_its_own_field():
    rows = [_row("g1", spend="1", roas=None, name="组-甲", campaign="B系列"),
            _row("g2", spend="1", roas=None, name="组-乙", campaign="A系列"),
            _row("g3", spend="1", roas=None, name="组-丙", campaign=None)]
    ordered = _sort_rows(rows, _filters("adgroup", sort_by="campaign_name", sort_direction="asc"))
    assert [row.row_key for row in ordered] == ["g2", "g1", "g3"]


def test_unknown_sort_field_is_rejected():
    from app.modules.reporting.filters import compile_filter

    with pytest.raises(ValueError):
        compile_filter(_filters("campaign", sort_by="unknown"))


def test_scheduled_report_is_account_wide_so_new_campaigns_are_not_missed(session, reporting_seed):
    """周期报表不按本地目录 ID 过滤，否则目录同步前新建的对象没有数据。"""
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    session.add(
        AdObject(
            tenant_id=route.tenant_id,
            advertiser_id="report-account",
            kind="campaign",
            remote_id="known-campaign",
            ad_type="REGULAR",
            operation_status="ENABLE",
            observed_at=datetime.now(UTC),
            published_version=1,
            source_connection_id=reporting_seed.connection.id,
            source_channel="OFFICIAL_API",
        )
    )
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    ensure_sync_schedules(session, context=reporting_seed.context, route=route, now=now)
    for schedule in session.exec(
        select(SyncSchedule).where(SyncSchedule.tenant_id == route.tenant_id)
    ).all():
        schedule.enabled = schedule.scope == "report"
    session.flush()
    enqueue_due_syncs(session, now=now)
    runs = session.exec(
        select(ReportSyncRun).where(ReportSyncRun.tenant_id == route.tenant_id)
    ).all()
    campaign_runs = [run for run in runs if run.query.get("report_contract") == "basic_campaign"]
    assert campaign_runs
    assert all(run.query["filter_ids"] == [] for run in campaign_runs)


def test_history_backlog_does_not_block_periodic_schedules(session, reporting_seed):
    route = FrozenTikTokRoute(
        tenant_id=reporting_seed.context.tenant_id,
        bc_id="bc-report",
        connection_id=reporting_seed.connection.id,
        channel="OFFICIAL_API",
        authorization_revision=0,
        adapter_contract_revision="official-api-v1",
        binding_revision=0,
    )
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    ensure_sync_schedules(session, context=reporting_seed.context, route=route, now=now)
    enqueue_due_syncs(session, now=now, include_history=False)
    # 只检查本用例账户的计划；共享测试库可能残留其他用例已提交的计划。
    own = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == route.tenant_id,
            SyncSchedule.advertiser_id == "report-account",
        )
    ).all()
    history = [row for row in own if row.scope == "history"]
    # 历史计划仍保持到期待领取，没有被推进或停用。
    assert history and all(row.enabled and row.last_request_id is None for row in history)
    report = [row for row in own if row.scope == "report"]
    assert report and all(row.last_request_id is not None for row in report)


class _Session:
    def __init__(self, row):
        self.row = row

    def exec(self, _statement):
        return SimpleNamespace(one_or_none=lambda: self.row)

    def flush(self):
        return None


def _sync_run():
    return SimpleNamespace(
        id=uuid4(), claim_generation=1, status="QUEUED", task_status=None, task_id=None,
        next_page=1, coverage="PENDING", next_attempt_at=None, retry_count=0,
        error_code=None, missing_reason=None, observed_at=None,
        query={
            "advertiser_id": "report-account", "report_contract": "basic_campaign",
            "metric_family": "delivery", "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": ["spend"], "start_date": "2026-09-01", "end_date": "2026-09-01",
            "granularity": "DAY", "currency": "USD", "timezone": "UTC",
            "attribution": "default", "filter_ids": [],
        },
    )


def _gateway(error: Exception):
    def read_page(_query):
        raise error

    return SimpleNamespace(reports=SimpleNamespace(read_page=read_page))


def test_rate_limit_deferral_waits_instead_of_failing():
    run = _sync_run()
    result = collect_report_step(
        _Session(run), run_id=run.id, claim_generation=1,
        gateway=_gateway(AccountAdmissionDeferred(15_000)),
    )
    assert result == "WAIT"
    assert run.status == "WAITING_REMOTE"
    assert run.retry_count == 0
    assert run.next_attempt_at >= datetime.now(UTC) + timedelta(seconds=14)


def test_provider_errors_retry_with_bound_then_fail():
    run = _sync_run()
    error = DomainError("mcp_business_error", "平台业务拒绝")
    for attempt in range(1, MAX_PROVIDER_RETRIES + 1):
        assert collect_report_step(
            _Session(run), run_id=run.id, claim_generation=1, gateway=_gateway(error)
        ) == "WAIT"
        assert run.retry_count == attempt
        run.status = "QUEUED"
    assert collect_report_step(
        _Session(run), run_id=run.id, claim_generation=1, gateway=_gateway(error)
    ) == "FAILED"
    assert run.status == "FAILED"


def test_contract_errors_still_fail_immediately():
    run = _sync_run()
    result = collect_report_step(
        _Session(run), run_id=run.id, claim_generation=1,
        gateway=_gateway(DomainError("report_query_invalid", "报表维度或指标未核验")),
    )
    assert result == "FAILED"
    assert run.retry_count == 0


def test_daylight_saving_day_bucket_matches_local_midnights():
    query = ReportQuery(
        advertiser_id="report-account", report_contract="basic_campaign", metric_family="delivery",
        dimensions=("campaign_id", "stat_time_day"), metrics=("spend",),
        start_date=date(2026, 3, 7), end_date=date(2026, 3, 9), granularity="DAY",
        currency="USD", timezone="America/New_York", attribution="default",
        filter_ids=(), page=1,
    )
    start, end = _bucket(query, {"stat_time_day": "2026-03-08 00:00:00"})
    # 2026-03-08 美东进入夏令时，当天只有 23 小时。
    assert end - start == timedelta(hours=23)
    assert end == datetime(2026, 3, 9, 4, tzinfo=UTC)


def test_scan_order_puts_recent_windows_before_history_backfill(report_case):
    """扫描在 SQL 中先取近期短窗口，历史分片（含按天拆出的旧素材分片）在后。"""
    from sqlmodel import col

    from app.modules.reporting import tasks

    report_case.seed_campaign("Provider-Drama", "report-account", 1, 1)
    today = date.today()
    windows = {
        "history-30d": (date(2026, 1, 1), date(2026, 1, 30)),
        "history-material-day": (date(2026, 2, 1), date(2026, 2, 1)),
        "core": (today - timedelta(days=1), today),
    }
    ids = {}
    for label, (start, end) in windows.items():
        run = _run(report_case, contract="basic_campaign", ad_type=None)
        run.status = "QUEUED"
        run.query = {"report_contract": "basic_campaign", "start_date": start.isoformat(),
                     "end_date": end.isoformat()}
        ids[run.id] = label
    report_case.session.flush()
    ordered = report_case.session.exec(
        select(ReportSyncRun)
        .where(col(ReportSyncRun.id).in_(ids))
        .order_by(tasks._SHORT_WINDOW.desc(), tasks._QUERY_END_DATE.desc(),
                  col(ReportSyncRun.created_at))
    ).all()
    assert [ids[row.id] for row in ordered] == ["core", "history-material-day", "history-30d"]
