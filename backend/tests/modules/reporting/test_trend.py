from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.modules.reporting.publish import (
    AccountMeta,
    mark_failed,
    publish_ad_day,
    publish_campaign_hour,
)
from app.modules.reporting.trend import build_trend
from tests.modules.reporting.sync_conftest import ad_row

D1 = date(2026, 10, 4)
D2 = date(2026, 10, 5)


def _seed(session, seed):
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D1,
        end=D2,
        rows=[
            ad_row("a1", D1, campaign="c1", spend="4.00", d0="1.00", total="2.00"),
            ad_row("a1", D2, campaign="c1", spend="6.00", d0="3.00", total="3.00"),
            ad_row("a2", D2, campaign="c2", spend="1.00", d0="1.00", total="1.00"),
        ],
    )


def _trend(session, seed, **kw):
    params = {
        "context": seed.context,
        "bc_id": "bc-a",
        "level": "campaign",
        "advertiser_ids": (),
        "ids": (),
        "start": D1,
        "end": D2,
        "granularity": "day",
    } | kw
    return build_trend(session, **params)


def test_day_trend_for_one_campaign(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, ids=("c1",))
    assert [p.at for p in trend.points] == ["2026-10-04", "2026-10-05"]
    assert [p.buckets[0].values["spend"] for p in trend.points] == [
        Decimal("4.00"),
        Decimal("6.00"),
    ]
    assert trend.points[1].buckets[0].values["roas_total"] == Decimal("0.5")


def test_account_level_sums_campaigns_per_day(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, level="account", advertiser_ids=("acc-1",))
    assert trend.points[1].buckets[0].values["spend"] == Decimal("7.00")
    # acc-1 两天都已完整同步，且只请求了 acc-1。
    assert [p.coverage for p in trend.points] == ["COMPLETE", "COMPLETE"]
    assert trend.points[1].buckets[0].availability["spend"] == "AVAILABLE"
    assert trend.timezone == "Asia/Hong_Kong"


def test_ad_level_uses_ad_facts(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, level="ad", ids=("a2",))
    assert [
        p.buckets[0].values["spend"] if p.buckets else None for p in trend.points
    ] == [None, Decimal("1.00")]


def test_empty_day_is_missing(session, sync_seed):
    trend = _trend(session, sync_seed, advertiser_ids=("acc-1",))
    assert [(p.at, p.buckets, p.coverage) for p in trend.points] == [
        ("2026-10-04", (), "MISSING"),
        ("2026-10-05", (), "MISSING"),
    ]
    assert trend.timezone == "UTC"


def test_incomplete_day_marks_partial(session, sync_seed):
    _seed(session, sync_seed)
    # D2 刷新失败：旧数据保留但状态变为 STALE，该点只能是不完整的部分合计。
    mark_failed(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_ids=["acc-1"],
        kind="ad_day",
        start=D2,
        end=D2,
        error_code="x",
    )
    trend = _trend(session, sync_seed, level="account", advertiser_ids=("acc-1",))
    assert [p.coverage for p in trend.points] == ["COMPLETE", "INCOMPLETE"]
    assert trend.points[0].buckets[0].availability["spend"] == "AVAILABLE"
    assert trend.points[1].buckets[0].availability["spend"] == "PARTIAL"
    assert trend.points[1].buckets[0].values["spend"] == Decimal("7.00")


def test_mixed_currency_yields_two_buckets(session, sync_seed):
    _seed(session, sync_seed)
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-2": AccountMeta("EUR", "UTC")},
        start=D2,
        end=D2,
        rows=[ad_row("b1", D2, campaign="c9", spend="5.00", adv="acc-2")],
    )
    trend = _trend(session, sync_seed, level="account")
    point = trend.points[1]
    assert [(b.currency, b.values["spend"]) for b in point.buckets] == [
        ("EUR", Decimal("5.00")),
        ("USD", Decimal("7.00")),
    ]
    assert point.coverage == "COMPLETE"
    # D1 只有 acc-1 完整，acc-2 无状态：不完整。
    assert trend.points[0].coverage == "INCOMPLETE"
    assert trend.timezone == "MIXED"


def test_hour_trend_filters_by_account_local_date(session, sync_seed):
    def hour(stamp, spend):
        return {
            "dimensions": {"campaign_id": "c1", "stat_time_hour": stamp},
            "metrics": {"advertiser_id": "acc-1", "spend": spend},
        }

    # 每次发布整天替换该账户本地日窗口，所以同一天的小时一次发布。
    for day, rows in (
        (D1, [hour("2026-10-04 23:00:00", "1")]),
        (D2, [hour("2026-10-05 00:00:00", "2"), hour("2026-10-05 07:00:00", "3")]),
    ):
        publish_campaign_hour(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            day=day,
            rows=rows,
        )
    trend = _trend(
        session,
        sync_seed,
        advertiser_ids=("acc-1",),
        ids=("c1",),
        start=D2,
        end=D2,
        granularity="hour",
    )
    # 香港 10-05 00:00 = UTC 10-04 16:00；10-04 23:00 本地日不在范围内。
    assert [(p.at, p.buckets[0].values["spend"]) for p in trend.points] == [
        ("2026-10-04T16:00+00:00", Decimal("2")),
        ("2026-10-04T23:00+00:00", Decimal("3")),
    ]
    assert {p.coverage for p in trend.points} == {"COMPLETE"}
    assert trend.timezone == "Asia/Hong_Kong"


def test_day_range_limit(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(session, sync_seed, start=D2 - timedelta(days=90), end=D2)
    assert err.value.status_code == 422


def test_hour_range_limit_and_reversed_range(session, sync_seed):
    for kw in (
        {"granularity": "hour", "start": D2 - timedelta(days=2), "end": D2},
        {"start": D2, "end": D1},
        {"ids": tuple(f"c{i}" for i in range(201))},
    ):
        with pytest.raises(HTTPException) as err:
            _trend(session, sync_seed, **kw)
        assert err.value.status_code == 422


def test_unauthorized_advertiser_is_forbidden(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(
            session, sync_seed, bc_id="bc-b", level="account", advertiser_ids=("acc-1",)
        )
    assert err.value.status_code == 403


def test_hour_only_for_account_and_campaign(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(session, sync_seed, level="ad", granularity="hour", start=D2, end=D2)
    assert err.value.status_code == 422


def test_trend_route_registered_with_stable_operation_id():
    from app.main import app

    route = app.openapi()["paths"]["/api/tenants/{tenant_id}/ads/trend"]["get"]
    assert route["operationId"] == "ads_reporting-ad_trend"


def test_trend_route_parses_query_and_rejects_bad_range(report_case, client):
    from app.api.deps import get_current_user
    from app.models import User

    app = client.app
    user = report_case.session.get(User, report_case.context.actor_id)
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        path = f"/api/tenants/{report_case.context.tenant_id}/ads/trend"
        params = {
            "bc_id": report_case.bc_id,
            "level": "campaign",
            "start_date": "2026-09-30",
            "end_date": "2026-10-01",
            "ids": "c1,,c2",
        }
        ok = client.get(path, params=params)
        assert ok.status_code == 200, ok.text
        assert [p["at"] for p in ok.json()["points"]] == ["2026-09-30", "2026-10-01"]
        bad = client.get(path, params=params | {"end_date": "2026-09-29"})
        assert bad.status_code == 422
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
