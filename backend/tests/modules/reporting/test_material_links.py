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


def refresh(session, seed, pages, ad_posts=None, progress=None):
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

    progress = {} if progress is None else progress
    for _ in range(1000):
        complete = refresh_material_links(
            session,
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            read_overview=read,
            read_ad_posts=read_ad_posts,
            now=NOW,
            progress=progress,
        )
        if complete:
            break
    else:
        raise AssertionError("关联扫描未结束")
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
    # 概览附带的无金额/无事实帖子不建立映射，不能把未追关联误记成已关联。
    assert links(session, sync_seed) == {("p1", "a1", "m1")}
    # 已删除广告的帖子查不到：记为未对照，之后不再因它整账户重扫。
    scan = session.get(MaterialLinkScan, (sync_seed.tenant_id, "acc-1", "unscoped"))
    assert scan.unresolved_ids == ["p9"]
    assert refresh(session, sync_seed, page) == []
    # 出现新的未对照帖子才重扫。
    publish(session, sync_seed, "acc-1", [material_row("p3", "2")])
    assert refresh(session, sync_seed, overview(("a3", "p3", "m3"))) == [1]
    assert ("p3", "a3", "m3") in links(session, sync_seed)


def test_replaced_post_name_does_not_prove_historical_video_identity(
    session, sync_seed
):
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
    # 当前目录只有同名视频并不能证明旧帖子使用了它；文件名允许跨内容重用。
    assert (rows["p1"].ad_remote_id, rows["p1"].platform_material_id) == ("a1", None)
    assert (rows["p2"].ad_material_id, rows["p2"].platform_material_id) == (
        "new-m",
        None,
    )
    # 未解析行每轮不能被 NULL→NULL 重写，避免历史缺口持续产生写放大。
    from sqlalchemy import text

    from app.modules.reporting.material_links import _resolve_videos

    query = text(
        "SELECT main_material_id,ctid::text FROM material_post_link ORDER BY 1"
    )
    before = session.connection().execute(query).all()
    _resolve_videos(session, sync_seed.tenant_id, "acc-1")
    assert session.connection().execute(query).all() == before


@pytest.mark.parametrize("has_name_candidate", [False, True])
@pytest.mark.parametrize("legacy_cursor", ["none", "checked", "locate"])
def test_missing_post_search_includes_same_period_report_ads(
    session, sync_seed, has_name_candidate, legacy_cursor
):
    from datetime import timedelta
    from uuid import uuid4

    from app.modules.reporting.store_models import AdReportDay

    publish(session, sync_seed, "acc-1", [material_row("old-post", "5", "old.mp4")])
    if has_name_candidate:
        use(session, sync_seed, adv="acc-1", ad="a-wrong", video="v1", ad_material="m1")
        session.exec(update(AdMaterialReference).values(name="old.mp4"))
    for ad, advertiser, day, automation in [
        ("b-owner", "acc-1", D, "UPGRADED_SMART_PLUS"),
        ("c-other-account", "acc-2", D, "UPGRADED_SMART_PLUS"),
        ("d-other-day", "acc-1", D - timedelta(days=1), "UPGRADED_SMART_PLUS"),
        ("e-regular", "acc-1", D, "MANUAL"),
    ]:
        session.add(
            AdReportDay(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=advertiser,
                stat_date=day,
                ad_id=ad,
                campaign_id="c1",
                adgroup_id="g1",
                campaign_automation_type=automation,
                currency="USD",
                timezone="UTC",
                fetched_at=NOW,
                sync_job_id=uuid4(),
                spend=Decimal(5),
            )
        )
    session.flush()
    # 0038在途工作可能已按旧的仅同名候选标记检查过；不能为它签发新范围的负证据。
    progress = {}
    if legacy_cursor != "none":
        progress["overview_complete"] = True
    if legacy_cursor == "checked":
        progress["checked_ids"] = ["old-post"]
    if legacy_cursor == "locate":
        progress["locate_post"] = {
            "mid": "old-post",
            "candidates": [],
            "next_batch": 0,
            "search_stack": [],
        }
    calls = refresh(
        session,
        sync_seed,
        overview(),
        ad_posts={"b-owner": ["old-post"]},
        progress=progress,
    )
    if legacy_cursor != "none":
        assert 1 not in calls
    assert ("old-post", "b-owner", None) in links(session, sync_seed)
    queried = {ad for call in calls if isinstance(call, tuple) for ad in call[1]}
    assert queried == ({"a-wrong", "b-owner"} if has_name_candidate else {"b-owner"})
    # 广告归属已经由平台证明；缺少精确视频证据时仍保留待解析，不能按名称猜 VID。
    row = session.exec(
        select(MaterialPostLink).where(MaterialPostLink.main_material_id == "old-post")
    ).one()
    assert row.platform_material_id is None


def test_current_evidence_continuation_keeps_completed_negative_search(
    session, sync_seed
):
    publish(session, sync_seed, "acc-1", [material_row("p1", "5", "same.mp4")])
    use(session, sync_seed, adv="acc-1", ad="a1", video="v1", ad_material="m1")
    session.exec(update(AdMaterialReference).values(name="same.mp4"))
    progress = {}
    assert not refresh_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda _page: overview(),
        read_ad_posts=lambda *_args: pytest.fail("概览阶段不应读取广告帖子"),
        now=NOW,
        progress=progress,
    )
    # 当前版本已完整核查的帖子续步不应被升级清理逻辑重查。
    progress["checked_ids"] = ["p1"]
    assert refresh(session, sync_seed, overview(), progress=progress) == []
    assert progress["checked_ids"] == ["p1"]


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


def replaced_video(session, seed):
    """ID 的字典序故意与同步先后相反，当前身份只能按目录观察版本判断。"""
    for mid, post, version, status in [
        ("z-old", "old-post", 3, "ENABLE"),
        ("a-current", "current-post", 8, "DISABLE"),
        (None, None, 9, "ENABLE"),
    ]:
        use(session, seed, adv="acc-1", ad="a1", video="v1", ad_material=mid, post=post)
        row = session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == seed.tenant_id,
                AdMaterialReference.ad_remote_id == "a1",
                AdMaterialReference.platform_material_id == "v1",
                AdMaterialReference.ad_material_id == mid,
            )
        ).one()
        row.published_version = version
        row.operation_status = status
        session.add(row)
    session.flush()


def test_ad_material_replacement_is_one_row_with_history_summed_once(
    session, sync_seed
):
    replaced_video(session, sync_seed)
    rows = [material_row("old-post", "10"), material_row("current-post", "4")]
    for row, total, day0 in zip(rows, ["20", "8"], ["10", "4"], strict=True):
        row["metrics"].update(
            {
                "onsite_total_ad_impression_value": total,
                "onsite_ad_impression_ad_revenue_day0": day0,
            }
        )
    publish(session, sync_seed, "acc-1", [*rows, material_row("other-ad-post", "100")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a2",
        video="v1",
        ad_material="other-ad",
        post="other-ad-post",
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
    assert len(result.items) == 1
    item = result.items[0]
    assert item.use_ref.ad_material_id == "a-current"
    assert item.operation_status == "DISABLE"
    assert item.post_ids == ("current-post", "old-post")
    assert item.metric_buckets[0].values["spend"] == Decimal("14")
    assert item.metric_buckets[0].values["revenue_total"] == Decimal("28")
    assert item.metric_buckets[0].values["revenue_d0"] == Decimal("14")
    assert item.metric_buckets[0].values["roas_total"] == Decimal("2")
    # 历史目录和帖子仍保留，避免丢掉过去的消耗。
    assert (
        len(
            session.exec(
                select(AdMaterialReference).where(
                    AdMaterialReference.tenant_id == sync_seed.tenant_id,
                    AdMaterialReference.ad_remote_id == "a1",
                )
            ).all()
        )
        == 3
    )


def test_ad_material_freeze_rejects_replaced_id_and_uses_current_id(session, sync_seed):
    replaced_video(session, sync_seed)
    kwargs = {
        "context": sync_seed.context,
        "bc_id": "bc-a",
        "advertiser_id": "acc-1",
        "ad_id": "a1",
    }
    with pytest.raises(HTTPException) as old:
        freeze_ad_material_selection(session, **kwargs, ad_material_ids=("z-old",))
    assert old.value.status_code == 404
    selection = freeze_ad_material_selection(
        session, **kwargs, ad_material_ids=("a-current",)
    )
    assert [u.ad_material_id for u in selection.material_uses] == ["a-current"]


def test_ad_material_ambiguous_current_ids_never_guess_an_operation_target(
    session, sync_seed
):
    replaced_video(session, sync_seed)
    session.exec(
        update(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == sync_seed.tenant_id,
            AdMaterialReference.ad_material_id == "z-old",
        )
        .values(published_version=8)
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
    assert len(result.items) == 1
    assert result.items[0].use_ref.ad_material_id is None
    assert result.items[0].operation_status is None
    for mid in ("z-old", "a-current"):
        with pytest.raises(HTTPException):
            freeze_ad_material_selection(
                session,
                context=sync_seed.context,
                bc_id="bc-a",
                advertiser_id="acc-1",
                ad_id="a1",
                ad_material_ids=(mid,),
            )


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
    from app.modules.reporting.material_links import _locate_post_step

    owners = {"p1": "ad-3", "p2": "ad-6"}
    queried = []

    def posts_of(ads, _first, _last, page):
        queried.append(ads)
        posts = {p for p, ad in owners.items() if ad in ads} | {"noise"}
        return RawReportPage(
            tuple({"dimensions": {"main_material_id": p}} for p in posts),
            page,
            1,
            len(posts),
        )

    result = {}
    for mid in owners:
        state = {
            "mid": mid,
            "candidates": [f"ad-{i}" for i in range(8)],
            "next_batch": 0,
            "search_stack": [],
        }
        for _ in range(10):
            complete, owner = _locate_post_step(
                state, read_ad_posts=posts_of, first=D, last=D
            )
            if complete:
                result[mid] = owner
                break
    assert result == owners
    # 推断得到的右侧单广告补一次直接验证；左侧单广告命中本身就是证据。
    assert len(queried) == 9
    assert ("ad-3",) in queried and ("ad-6",) in queried


def test_preferred_candidate_does_not_expand_into_report_candidates():
    from app.modules.reporting.material_links import _locate_post_step

    state = {
        "mid": "p",
        "candidates": ["owner"] + [f"extra-{i}" for i in range(199)],
        "preferred_count": 1,
        "next_batch": 0,
        "search_stack": [],
    }
    calls = []

    def read(ads, _first, _last, page):
        calls.append(ads)
        assert ads == ("owner",)
        return RawReportPage(({"dimensions": {"main_material_id": "p"}},), page, 1, 1)

    assert _locate_post_step(state, read_ad_posts=read, first=D, last=D) == (
        True,
        "owner",
    )
    assert calls == [("owner",)]


def test_truncated_candidate_batch_splits_and_preserves_unsearched_half():
    from app.modules.reporting.material_links import _locate_post_step

    state = {
        "mid": "p",
        "candidates": ["a", "b", "c", "owner"],
        "preferred_count": 0,
        "next_batch": 0,
        "search_stack": [],
    }
    calls = []

    def read(ads, _first, _last, page):
        calls.append(ads)
        if len(ads) == 4:
            return RawReportPage((), page, 200, 20_000)
        rows = ({"dimensions": {"main_material_id": "p"}},) if "owner" in ads else ()
        return RawReportPage(rows, page, 1 if rows else 0, len(rows))

    for _ in range(6):
        complete, owner = _locate_post_step(state, read_ad_posts=read, first=D, last=D)
        if complete:
            break
    assert complete and owner == "owner"
    assert calls == [
        ("a", "b", "c", "owner"),
        ("a", "b"),
        ("c", "owner"),
        ("c",),
        ("owner",),
    ]


def test_new_directory_retries_cached_unresolved_posts(session, sync_seed):
    # 首次素材报表先于目录：当时无法找到同名素材，旧负缓存不能永久挡住后来的目录。
    publish(session, sync_seed, "acc-1", [material_row("late-post", "30", "late.mp4")])
    assert refresh(session, sync_seed, overview()) == [1]
    assert refresh(session, sync_seed, overview()) == []
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="late-ad",
        video="late-video",
        ad_material="late-material",
    )
    session.exec(
        update(AdMaterialReference)
        .where(
            AdMaterialReference.tenant_id == sync_seed.tenant_id,
            AdMaterialReference.advertiser_id == "acc-1",
            AdMaterialReference.ad_remote_id == "late-ad",
        )
        .values(name="late.mp4")
    )
    calls = refresh(session, sync_seed, overview(), ad_posts={"late-ad": ["late-post"]})
    assert calls == [1, ("ads", ("late-ad",), D, D)]
    link = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == sync_seed.tenant_id,
            MaterialPostLink.main_material_id == "late-post",
        )
    ).one()
    assert link.ad_remote_id == "late-ad" and link.platform_material_id is None


def test_single_ad_response_resolves_all_matching_posts_together(session, sync_seed):
    posts = [f"old-{i}" for i in range(20)]
    publish(
        session, sync_seed, "acc-1", [material_row(p, "1", "v1.mp4") for p in posts]
    )
    use(session, sync_seed, adv="acc-1", ad="a1", video="v1", ad_material="new-id")
    calls = refresh(session, sync_seed, overview(), ad_posts={"a1": posts})
    assert calls == [1, ("ads", ("a1",), D, D)]
    rows = session.exec(
        select(MaterialPostLink).where(
            MaterialPostLink.tenant_id == sync_seed.tenant_id
        )
    ).all()
    assert len(rows) == 20
    assert {r.ad_remote_id for r in rows} == {"a1"}
    assert {r.platform_material_id for r in rows} == {None}
