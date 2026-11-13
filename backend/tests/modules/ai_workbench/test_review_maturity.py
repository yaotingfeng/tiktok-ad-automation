"""成熟窗口必须查询独立历史事实，不能给近七日累计换一个 D7 名称。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from app.modules.ai_workbench import service
from app.modules.ai_workbench.review_data import account_window, build_data_pack
from app.modules.ai_workbench.reviews import load_optimizer
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.reporting.sync_conftest import ad_row

CUTOFF = datetime(2026, 11, 13, 12, tzinfo=UTC)


def _version(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    return load_optimizer(session, context, optimizer.id)[1]


def _publish(session, context, *, missing_d7=False, observed=CUTOFF):
    start, end = date(2026, 10, 30), date(2026, 11, 12)
    rows = []
    for offset in range(14):
        day = start + timedelta(days=offset)
        row = ad_row(
            "ad-one", day, campaign="campaign-one", spend="10", d0="1", total="999"
        )
        row["metrics"]["native_growth_ad_revenue_value_d2"] = "3"
        row["metrics"]["native_growth_ad_revenue_value_d6"] = (
            None if missing_d7 else ("7" if day <= date(2026, 11, 5) else "900")
        )
        rows.append(row)
    publish_ad_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=start,
        end=end,
        rows=rows,
        requested_at=observed,
    )


def _campaign(pack, horizon):
    return next(
        row
        for row in pack["reporting"]
        if row["dimension"] == "campaign" and row["maturity_horizon"] == horizon
    )


def test_maturity_windows_cover_seven_complete_local_days_each():
    asia = account_window("Asia/Hong_Kong", datetime(2026, 11, 13, 2, tzinfo=UTC))
    america = account_window(
        "America/Los_Angeles", datetime(2026, 11, 13, 2, tzinfo=UTC)
    )
    assert asia["maturity_windows"]["d7"] == {
        "start_date": "2026-10-30",
        "end_date": "2026-11-05",
        "maturity_days": 7,
    }
    assert america["maturity_windows"]["d7"]["end_date"] == "2026-11-04"
    assert america["maturity_windows"]["d3"]["start_date"] == "2026-11-02"


def test_mature_d7_queries_history_excludes_immature_revenue_and_freezes(
    session, context
):
    version = _version(session, context)
    _publish(session, context)
    pack = build_data_pack(session, context=context, version=version, cutoff=CUTOFF)
    assert len(pack["reporting"]) == 12
    d7 = _campaign(pack, "d7")
    assert d7["filters"]["start_date"] == "2026-10-30"
    assert d7["filters"]["end_date"] == "2026-11-05"
    bucket = d7["rows"][0]["metric_buckets"][0]
    assert bucket["values"]["spend"] == "70"
    assert bucket["values"]["revenue_d7"] == "49"
    assert bucket["values"]["roas_d7"] == "0.7"
    assert d7["maturity_evidence"]["status"] == "AVAILABLE"
    assert d7["maturity_evidence"]["observed_after_maturity_days"] == 7
    assert (
        d7["maturity_evidence"]["revenue_metric"]["platform_field"]
        == "native_growth_ad_revenue_value_d6"
    )
    assert _campaign(pack, "d0")["filters"]["start_date"] == "2026-11-06"
    _publish(session, context, missing_d7=True, observed=CUTOFF + timedelta(seconds=1))
    assert bucket["values"]["revenue_d7"] == "49"


def test_missing_d7_never_falls_back_to_generic_revenue(session, context):
    version = _version(session, context)
    _publish(session, context, missing_d7=True)
    d7 = _campaign(
        build_data_pack(session, context=context, version=version, cutoff=CUTOFF), "d7"
    )
    assert d7["rows"][0]["metric_buckets"][0]["values"]["revenue_d7"] is None
    assert d7["maturity_evidence"]["revenue_metric"]["availability"] == "MISSING"
    assert d7["maturity_evidence"]["status"] == "MISSING"


def test_old_observation_does_not_establish_mature_d7(session, context):
    version = _version(session, context)
    _publish(session, context, observed=datetime(2026, 11, 6, tzinfo=UTC))
    d7 = _campaign(
        build_data_pack(session, context=context, version=version, cutoff=CUTOFF), "d7"
    )
    evidence = d7["maturity_evidence"]
    assert evidence["status"] == "STALE"
    assert evidence["observed_after_maturity_days"] == 0
    assert evidence["oldest_observed_at"].startswith("2026-11-06")
    assert len(evidence["days"]) == 7


def test_missing_daily_coverage_and_unsupported_material_d3_are_explicit(
    session, context
):
    from sqlmodel import select

    from app.modules.reporting.store_models import ReportSyncState

    version = _version(session, context)
    _publish(session, context)
    state = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
            ReportSyncState.advertiser_id == "acc-1",
            ReportSyncState.report_kind == "ad_day",
            ReportSyncState.stat_date == date(2026, 10, 30),
        )
    ).one()
    session.delete(state)
    session.flush()
    pack = build_data_pack(session, context=context, version=version, cutoff=CUTOFF)
    evidence = _campaign(pack, "d7")["maturity_evidence"]
    assert evidence["status"] == "PARTIAL"
    assert evidence["complete_days"] == 6
    assert evidence["days"][0]["status"] == "MISSING"
    material = next(
        row
        for row in pack["reporting"]
        if row["dimension"] == "material" and row["maturity_horizon"] == "d3"
    )
    assert (
        material["maturity_evidence"]["revenue_metric"]["availability"] == "UNSUPPORTED"
    )
    assert material["maturity_evidence"]["revenue_metric"]["platform_field"] is None
