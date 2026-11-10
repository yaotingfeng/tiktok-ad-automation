"""审查回归：没有可归属创意时仍保留覆盖缺口，显示名称与排序一致。"""

import json
from decimal import Decimal

import pytest

from app.modules.reporting.ad_materials import list_ad_materials
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.trend import build_trend
from tests.modules.reporting.query_helpers import query_ads
from tests.modules.reporting.test_creative_projection import build, identity
from tests.modules.reporting.test_creative_reporting import D, creative_row
from tests.modules.reporting.test_material_query import use


@pytest.mark.parametrize("unknown", [False, True])
def test_empty_ad_attribution_stays_incomplete_everywhere(session, sync_seed, unknown):
    use(session, sync_seed, adv="acc-1", ad="ad-1", video="vid-1", ad_material="am-1")
    build(
        session,
        sync_seed,
        rows=[creative_row("unknown", "100", "30")] if unknown else [],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        ids=("ad-1",),
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert page.total == 0
    assert page.coverage["status"] == "INCOMPLETE"
    assert page.summary["incomplete_advertiser_count"] == 1
    assert page.coverage["unallocated_group_count"] == 1
    full = query_ads(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=filters.model_copy(update={"include_empty": True}),
    )
    assert full.total == 1
    assert full.items[0].coverage["status"] == "INCOMPLETE"
    assert full.items[0].metric_buckets[0].values["spend"] is None
    assert full.items[0].metric_buckets[0].availability["spend"] != "AVAILABLE"
    assert full.summary["buckets"][0]["values"]["spend"] is None
    detail = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="ad-1",
        start=D,
        end=D,
    )
    assert detail.coverage["status"] == "INCOMPLETE"
    assert detail.items[0].metric_buckets[0].values["spend"] is None
    for keys in ((), (json.dumps(["material", "VIDEO", "vid-1"]),)):
        trend = build_trend(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            level="material",
            advertiser_ids=("acc-1",),
            ids=keys,
            start=D,
            end=D,
            granularity="day",
            snapshot_id=page.snapshot.snapshot_id,
        )
        assert trend.points[0].coverage == "INCOMPLETE"
        assert all(b.values["spend"] is None for b in trend.points[0].buckets)


def test_special_names_sort_in_rendered_order_across_frozen_pages(session, sync_seed):
    for creative, name in [("1", "Z"), ("2", "A")]:
        item = identity(
            session,
            sync_seed,
            creative,
            assets=[
                {"material_type": "IMAGE", "platform_material_id": "i1"},
                {"material_type": "IMAGE", "platform_material_id": "i2"},
            ],
        )
        item.name = name
        session.add(item)
    normal = identity(session, sync_seed, "normal")
    normal.name = "M normal"
    session.add(normal)
    build(
        session,
        sync_seed,
        ad_spend="30",
        rows=[creative_row(c, "10", "10") for c in ("1", "2", "normal")],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        min_spend=Decimal(1),
        sort_by="material_name",
        sort_direction="asc",
    )
    args = {
        "context": sync_seed.context,
        "bc_id": "bc-a",
        "filters": filters,
        "limit": 1,
    }
    page = query_ads(session, **args)
    names = [page.items[0].display["name"]]
    while page.next_cursor:
        page = query_ads(
            session,
            **args,
            snapshot_id=page.snapshot.snapshot_id,
            cursor=page.next_cursor,
        )
        names.append(page.items[0].display["name"])
    assert names == ["M normal", "轮播创意 1", "轮播创意 2"]


def test_unallocated_coverage_does_not_cross_filtered_groups_or_scope(
    session, sync_seed
):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.ads.models import AdObject
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_creative_day,
    )
    from tests.modules.reporting.sync_conftest import ad_row

    for ad, group in (("bad", "g1"), ("good", "g2")):
        use(
            session,
            sync_seed,
            adv="acc-1",
            ad=ad,
            video=ad,
            ad_material=ad,
            adgroup=group,
        )
    bad = session.get(AdObject, (sync_seed.tenant_id, "acc-1", "ad", "bad"))
    bad.operation_status = "DISABLE"
    session.add(bad)
    good = ad_row("good", D, adgroup="g2", spend="0")
    for key in good["metrics"]:
        if key.startswith("native_growth") or key in {"impressions", "clicks"}:
            good["metrics"][key] = "0"
    accounts = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("bad", D, spend="100"), good],
    )
    publish_creative_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[],
    )
    for changes in (
        {"ids": ("good",)},
        {"query": "广告good"},
        {"operation_statuses": ("ENABLE",)},
        {"ids": ("bad",), "operation_statuses": ("ENABLE",)},
    ):
        filters = ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1",),
            include_empty=True,
        ).model_copy(update=changes)
        page = query_ads(
            session, context=sync_seed.context, bc_id="bc-a", filters=filters
        )
        assert page.coverage["unallocated_group_count"] == 0
        assert page.summary["incomplete_advertiser_count"] == 0
        assert all(row.metric_buckets[0].values["spend"] == 0 for row in page.items)
    # 相同组名的其他授权账户及日期也不能继承这个账户/日期的缺口。
    for filters in (
        ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-2",),
            ids=("bad",),
        ),
        ReportingFilter(
            dimension="material",
            start_date=D + timedelta(days=1),
            end_date=D + timedelta(days=1),
            advertiser_ids=("acc-1",),
            ids=("bad",),
        ),
    ):
        page = query_ads(
            session, context=sync_seed.context, bc_id="bc-a", filters=filters
        )
        assert page.coverage["unallocated_group_count"] == 0


def test_unknown_group_marker_keeps_attributed_amount_without_assigning_unknown_money(
    session, sync_seed
):
    use(session, sync_seed, adv="acc-1", ad="ad-1", video="vid-1", ad_material="am-1")
    identity(session, sync_seed, "known")
    build(
        session,
        sync_seed,
        rows=[creative_row("known", "20", "10"), creative_row("unknown", "80", "20")],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        advertiser_ids=("acc-1",),
        ids=("ad-1",),
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    assert page.total == 1
    assert page.items[0].metric_buckets[0].values["spend"] == 20
    assert page.items[0].metric_buckets[0].availability["spend"] == "PARTIAL"
    assert Decimal(page.summary["buckets"][0]["values"]["spend"]) == 20
    assert page.summary["buckets"][0]["availability"]["spend"] == "PARTIAL"


@pytest.mark.parametrize("missing_currency", ["USD", "EUR"])
def test_unallocated_days_preserve_currency_and_timezone_buckets(
    session, sync_seed, missing_currency
):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_creative_day,
    )
    from tests.modules.reporting.sync_conftest import ad_row

    use(session, sync_seed, adv="acc-1", ad="ad-1", video="vid-1", ad_material="am-1")
    identity(session, sync_seed, "known")
    build(session, sync_seed, ad_spend="20", rows=[creative_row("known", "20", "30")])
    later = D + timedelta(days=1)
    unknown = creative_row("unknown", "100", "30")
    unknown["dimensions"]["stat_time_day"] = f"{later} 00:00:00"
    accounts = {"acc-1": AccountMeta(missing_currency, "UTC")}
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=later,
        end=later,
        rows=[ad_row("ad-1", later, spend="100", total="30")],
    )
    publish_creative_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=later,
        end=later,
        rows=[unknown],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=later,
        advertiser_ids=("acc-1",),
        ids=("ad-1",),
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    detail = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="ad-1",
        start=D,
        end=later,
    )
    for buckets in (
        [b.model_dump(mode="json") for b in page.items[0].metric_buckets],
        page.summary["buckets"],
        [b.model_dump(mode="json") for b in detail.items[0].metric_buckets],
    ):
        by_currency = {b["currency"]: b for b in buckets}
        assert Decimal(by_currency["USD"]["values"]["spend"]) == 20
        if missing_currency == "USD":
            assert by_currency["USD"]["timezone"] == "MIXED"
        else:
            assert by_currency["USD"]["timezone"] == "Asia/Hong_Kong"
            assert by_currency["EUR"]["timezone"] == "UTC"
            assert by_currency["EUR"]["values"]["spend"] is None


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("missing_video", ["vid-1", "other-vid"])
def test_missing_account_contribution_is_not_hidden_by_shared_currency_bucket(
    session, sync_seed, unknown, missing_video
):
    from uuid import uuid4

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.reporting.publish import (
        AccountMeta,
        publish_ad_day,
        publish_creative_day,
    )
    from tests.modules.reporting.sync_conftest import ad_row

    use(session, sync_seed, adv="acc-1", ad="ad-1", video="vid-1", ad_material="am-1")
    identity(session, sync_seed, "known")
    build(session, sync_seed, ad_spend="20", rows=[creative_row("known", "20", "30")])
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="ad-2",
        video=missing_video,
        ad_material="am-2",
    )
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-2"))
    account.timezone = "Asia/Hong_Kong"
    session.add(account)
    accounts = {"acc-2": AccountMeta("USD", "Asia/Hong_Kong")}
    missing = creative_row("unknown", "80", "40")
    missing["metrics"]["advertiser_id"] = "acc-2"
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[ad_row("ad-2", D, adv="acc-2", spend="80", total="40")],
    )
    publish_creative_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts=accounts,
        start=D,
        end=D,
        rows=[missing] if unknown else [],
    )
    filters = ReportingFilter(
        dimension="material",
        start_date=D,
        end_date=D,
        include_empty=True,
        advertiser_ids=("acc-1", "acc-2"),
        ids=("ad-1", "ad-2"),
    )
    page = query_ads(session, context=sync_seed.context, bc_id="bc-a", filters=filters)
    video = next(row for row in page.items if json.loads(row.row_key)[2] == "vid-1")
    assert page.summary["incomplete_advertiser_count"] == 1
    assert page.summary["buckets"][0]["availability"]["spend"] == "PARTIAL"
    for metric, expected in (("spend", 20), ("revenue_total", 30)):
        assert video.metric_buckets[0].values[metric] == expected
        assert Decimal(page.summary["buckets"][0]["values"][metric]) == expected
        assert video.metric_buckets[0].availability[metric] == (
            "PARTIAL" if missing_video == "vid-1" else "AVAILABLE"
        )
    if missing_video != "vid-1":
        other = next(
            row for row in page.items if json.loads(row.row_key)[2] == missing_video
        )
        assert other.metric_buckets[0].values["spend"] is None

    # 收窄账户、广告或不同 VID 后，其他账户的缺口不能污染完整的 A。
    isolated_filters = [
        filters.model_copy(update={"advertiser_ids": ("acc-1",)}),
        filters.model_copy(update={"ids": ("ad-1",)}),
    ]
    if missing_video != "vid-1":
        isolated_filters.append(filters.model_copy(update={"query": "vid-1"}))
    for isolated in isolated_filters:
        result = query_ads(
            session, context=sync_seed.context, bc_id="bc-a", filters=isolated
        )
        assert result.total == 1
        assert result.items[0].metric_buckets[0].values["spend"] == 20
        assert result.items[0].metric_buckets[0].availability["spend"] == "AVAILABLE"
        assert Decimal(result.summary["buckets"][0]["values"]["spend"]) == 20
        assert result.summary["buckets"][0]["availability"]["spend"] == "AVAILABLE"
        assert result.summary["incomplete_advertiser_count"] == 0
