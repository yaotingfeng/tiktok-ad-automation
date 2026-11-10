import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlmodel import select, update

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.ad_materials import (
    freeze_ad_material_selection,
    list_ad_materials,
)
from app.modules.reporting.trend import build_trend
from tests.modules.reporting.query_helpers import finish_projections
from tests.modules.reporting.test_material_query import material_row, publish, use

D = date(2026, 10, 5)
NOW = datetime(2026, 10, 6, tzinfo=UTC)


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
    finish_projections(session)
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
    assert by_material["m1"].creative_ids == ("p1",)
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


def test_ad_material_replacement_is_one_row_with_history_summed_once(
    session, sync_seed
):
    replaced_video(session, sync_seed)
    rows = [material_row("old-post", "10"), material_row("current-post", "4")]
    for row, total, day0 in zip(rows, ["20", "8"], ["10", "4"], strict=True):
        row["metrics"].update(
            {
                "native_growth_total_ad_impression_value": total,
                "native_growth_ad_revenue_value_d0": day0,
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
    finish_projections(session)
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
    assert item.creative_ids == ("current-post",)
    assert sum(
        r.metric_buckets[0].values["spend"] for r in result.creative_rows
    ) == Decimal("14")
    assert item.metric_buckets[0].values["spend"] == Decimal("4")
    assert item.metric_buckets[0].values["revenue_total"] == Decimal("8")
    assert item.metric_buckets[0].values["revenue_d0"] == Decimal("4")
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
    finish_projections(session)
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
    finish_projections(session)
    trend = build_trend(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        level="material",
        advertiser_ids=("acc-1",),
        ids=(json.dumps(["material", "VIDEO", "v1"]),),
        start=D,
        end=D,
        granularity="day",
    )
    assert trend.points[0].buckets[0].values["spend"] == Decimal(14)


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
