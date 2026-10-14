from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlmodel import col, select, update

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.ad_materials import (
    freeze_ad_material_selection,
    list_ad_materials,
)
from app.modules.reporting.jobs import _read_all
from app.modules.reporting.material_links import refresh_material_links
from app.modules.reporting.store_models import MaterialLinkScan, MaterialPostLink
from app.modules.reporting.trend import build_trend
from tests.modules.reporting.test_material_query import material_row, publish, use

D = date(2026, 10, 5)
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def overview(*pairs):
    """创意概览“广告 × 主素材”行：(广告, 帖子, 广告内素材 ID[, 文件名])。"""
    return RawReportPage(
        tuple(
            {
                "dimensions": {
                    "smart_plus_ad_id": ad,
                    "main_material_id": post,
                    "main_material_type": "VIDEO_SPARK_ADS",
                },
                "metrics": {
                    "spend": "0",
                    "ad_material_id": ad_material,
                    "main_material_name": name,
                },
            }
            for ad, post, ad_material, *rest in pairs
            for name in (rest[0] if rest else None,)
        ),
        1,
        1,
        len(pairs),
    )


def refresh(session, seed, pages, ad_posts=None):
    calls = []

    def read(page):
        calls.append(page)
        return pages

    def read_ad_posts(ads, first, last, _page):
        # 按广告查拆分报表：返回这些广告下（含已被替换的）帖子。
        calls.append(("ads", ads, first, last))
        posts = [p for ad in ads for p in (ad_posts or {}).get(ad, [])]
        return RawReportPage(
            tuple({"dimensions": {"main_material_id": p}} for p in posts),
            1,
            1 if posts else 0,
            len(posts),
        )

    refresh_material_links(
        session,
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=read,
        read_ad_posts=read_ad_posts,
        read_all=_read_all,
        now=NOW,
    )
    return calls


def links(session, seed):
    return {
        (link.main_material_id, link.ad_remote_id, link.ad_material_id)
        for link in session.exec(
            select(MaterialPostLink).where(MaterialPostLink.tenant_id == seed.tenant_id)
        )
    }


def test_refresh_links_only_when_new_posts_appear(session, sync_seed):
    publish(
        session, sync_seed, "acc-1", [material_row("p1", "10"), material_row("p9", "1")]
    )
    page = overview(("a1", "p1", "m1"), ("a1", "p2", "m2"))
    assert refresh(session, sync_seed, page) == [1]
    assert links(session, sync_seed) == {("p1", "a1", "m1"), ("p2", "a1", "m2")}
    # 已删除广告的帖子查不到：记为未对照，之后不再因它整账户重扫。
    scan = session.get(MaterialLinkScan, (sync_seed.tenant_id, "acc-1"))
    assert scan.unresolved_ids == ["p9"]
    assert refresh(session, sync_seed, page) == []
    # 出现新的未对照帖子才重扫。
    publish(session, sync_seed, "acc-1", [material_row("p3", "2")])
    assert refresh(session, sync_seed, overview(("a3", "p3", "m3"))) == [1]
    assert ("p3", "a3", "m3") in links(session, sync_seed)


def test_replaced_post_located_by_ad_and_video_resolved_by_name(session, sync_seed):
    # 广告内素材重建：概览只列新帖子 p2，旧帖子 p1 仍有消耗；目录也还停在旧 ID。
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "5", "s7.mp4"), material_row("p2", "1", "s7.mp4")],
    )
    use(session, sync_seed, adv="acc-1", ad="a1", video="v7", ad_material="old-m")
    session.exec(
        update(AdMaterialReference)
        .where(col(AdMaterialReference.ad_material_id) == "old-m")
        .values(name="s7.mp4")
    )
    calls = refresh(
        session,
        sync_seed,
        overview(("a1", "p2", "new-m", "s7.mp4")),
        ad_posts={"a1": ["p1", "p2"]},
    )
    assert calls == [1, ("ads", ("a1",), D, D)]
    rows = {
        link.main_material_id: link
        for link in session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == sync_seed.tenant_id
            )
        )
    }
    assert (rows["p1"].ad_remote_id, rows["p1"].platform_material_id) == ("a1", "v7")
    assert (rows["p2"].ad_material_id, rows["p2"].platform_material_id) == (
        "new-m",
        "v7",
    )


def test_ad_detail_materials_are_scoped_to_the_ad(session, sync_seed):
    # 同一视频在两条广告：广告详情只看本广告的那条帖子，不混入另一条广告。
    publish(
        session, sync_seed, "acc-1", [material_row("p1", "10"), material_row("p2", "4")]
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(session, sync_seed, adv="acc-1", ad="a1", video="v2", ad_material="m2")
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v1",
        ad_material="m3",
        post="p2",
    )
    result = list_ad_materials(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="a1",
        start=D,
        end=D,
    )
    by_material = {item.use_ref.ad_material_id: item for item in result.items}
    assert set(by_material) == {"m1", "m2"}
    assert by_material["m1"].metric_buckets[0].values["spend"] == Decimal(10)
    assert by_material["m1"].post_ids == ("p1",)
    assert by_material["m2"].metric_buckets[0].values["spend"] == Decimal(0)
    with pytest.raises(HTTPException) as foreign:
        list_ad_materials(
            session,
            context=sync_seed.context,
            bc_id="bc-b",
            advertiser_id="acc-1",
            ad_id="a1",
            start=D,
            end=D,
        )
    assert foreign.value.status_code == 404


def test_freeze_ad_material_selection_targets_only_this_ad(session, sync_seed):
    use(session, sync_seed, adv="acc-1", ad="a1", video="v1", ad_material="m1")
    use(session, sync_seed, adv="acc-1", ad="a2", video="v1", ad_material="m2")
    selection = freeze_ad_material_selection(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        advertiser_id="acc-1",
        ad_id="a1",
        ad_material_ids=("m1",),
    )
    assert selection.refs == ()
    assert [
        (u.ad_ref.remote_id, u.ad_material_id) for u in selection.material_uses
    ] == [("a1", "m1")]
    # 其他广告的素材不能借本广告的入口选中。
    with pytest.raises(HTTPException) as other:
        freeze_ad_material_selection(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            advertiser_id="acc-1",
            ad_id="a1",
            ad_material_ids=("m2",),
        )
    assert other.value.status_code == 404


def test_material_trend_by_video_sums_its_posts(session, sync_seed):
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10"), material_row("p2", "4"), material_row("p3", "100")],
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v1",
        ad_material="m2",
        post="p2",
    )
    trend = build_trend(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        level="material",
        advertiser_ids=("acc-1",),
        ids=("v1",),
        start=D,
        end=D,
        granularity="day",
    )
    assert trend.points[0].buckets[0].values["spend"] == Decimal(14)


def test_locate_posts_splits_only_batches_with_targets():
    from app.modules.reporting.material_links import _locate_posts

    owners = {"p1": "ad-3", "p2": "ad-6"}
    queried = []

    def posts_of(ads):
        queried.append(ads)
        return {p for p, ad in owners.items() if ad in ads} | {"noise"}

    ads = [f"ad-{i}" for i in range(8)]
    assert _locate_posts(ads, {"p1", "p2"}, posts_of) == owners
    # 8 个广告、2 个目标：整批 1 次 + 首层左半 1 次 + 每个目标往下每层 1 次，共 6 次。
    assert len(queried) == 6
