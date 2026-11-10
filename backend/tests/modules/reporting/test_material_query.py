import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.modules.ads.creative_models import AdCreative
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.reporting.publish import AccountMeta, publish_creative_day
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    CreativeReportDay,
    ReportSyncState,
)
from tests.modules.reporting.query_helpers import build_rows

D = date(2026, 10, 5)


def material_row(creative_id, spend, _name=None):
    from tests.modules.reporting.test_creative_reporting import creative_row

    row = creative_row(creative_id, spend, "0")
    for key in row["metrics"]:
        if key.startswith("native_growth") or key in {"impressions", "clicks"}:
            row["metrics"][key] = "0"
    return row


def reconcile(session, seed, adv):
    # 查询夹具的广告总账恰好等于创意总账；差额专项另有独立用例。
    session.flush()
    columns = ",".join(METRIC_COLUMNS)
    params = {"t": seed.tenant_id, "a": adv, "d": D}
    session.execute(
        text(
            "DELETE FROM ad_report_day WHERE tenant_id=:t AND advertiser_id=:a AND stat_date=:d"
        ),
        params,
    )
    session.execute(
        text(f"""INSERT INTO ad_report_day
        (tenant_id,advertiser_id,stat_date,ad_id,campaign_id,adgroup_id,currency,timezone,fetched_at,sync_job_id,{columns})
        SELECT tenant_id,advertiser_id,stat_date,creative_id,campaign_id,adgroup_id,currency,timezone,fetched_at,sync_job_id,{columns}
        FROM creative_report_day WHERE tenant_id=:t AND advertiser_id=:a AND stat_date=:d"""),
        params,
    )
    session.merge(
        ReportSyncState(
            tenant_id=seed.tenant_id,
            advertiser_id=adv,
            report_kind="ad_day",
            stat_date=D,
            status="COMPLETE",
            observed_at=datetime.now(UTC),
            last_success_at=datetime.now(UTC),
            publication_version=1,
        )
    )
    session.flush()


def publish(session, seed, adv, rows):
    for row in rows:
        row["metrics"]["advertiser_id"] = adv
    publish_creative_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={adv: AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=rows,
    )
    reconcile(session, seed, adv)


def use(session, seed, *, adv, ad, video, ad_material, post=None, adgroup="g1"):
    """当前唯一使用位置及创意身份；旧夹具参数名 post 现在表示 creative_id。"""
    now = datetime.now(UTC)
    session.add(
        AdMaterialReference(
            tenant_id=seed.tenant_id,
            advertiser_id=adv,
            ad_remote_id=ad,
            platform_material_id=video,
            ad_material_id=ad_material,
            material_type="VIDEO",
            name=f"{video}.mp4",
            complete=True,
            published_version=1,
        )
    )
    if post:
        session.merge(
            AdCreative(
                tenant_id=seed.tenant_id,
                advertiser_id=adv,
                creative_id=post,
                ad_remote_id=ad,
                ad_material_id=ad_material,
                material_type="VIDEO",
                platform_material_id=video,
                identity_status="RESOLVED",
                assets=[{"material_type": "VIDEO", "platform_material_id": video}],
                name=f"{video}.mp4",
                first_observed_at=now,
                observed_at=now,
                published_version=1,
            )
        )
        fact = session.get(CreativeReportDay, (seed.tenant_id, adv, D, post))
        if fact:
            fact.adgroup_id = adgroup
            session.add(fact)
            reconcile(session, seed, adv)
    for kind, remote_id, parent, name, status in (
        ("ad", ad, adgroup, f"广告{ad}", "ENABLE"),
        ("adgroup", adgroup, "c1", f"组{adgroup}", "DISABLE"),
    ):
        if session.get(AdObject, (seed.tenant_id, adv, kind, remote_id)) is None:
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id=adv,
                    kind=kind,
                    remote_id=remote_id,
                    parent_kind="adgroup" if kind == "ad" else "campaign",
                    parent_remote_id=parent,
                    ad_type="SMART_PLUS",
                    name=name,
                    operation_status=status,
                    observed_at=now,
                    published_version=1,
                )
            )
    session.flush()


def query(session, seed, **kw):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="material",
            start_date=D,
            end_date=D,
            **({"include_empty": True} | kw),
        ),
    )


def by_id(rows):
    return {r.display["main_material_id"]: r for r in rows}


def test_page_coverage_does_not_rescan_all_accounts_for_every_row(
    session, sync_seed, monkeypatch
):
    from app.modules.reporting import material_query

    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
    publish(session, sync_seed, "acc-2", [material_row("p2", "20")])
    original = material_query.creative_states
    visits = []
    sizes = []

    class CountedStates(list):
        def __iter__(self):
            for item in super().__iter__():
                visits.append(1)
                yield item

    def counted(*args, **kwargs):
        states = original(*args, **kwargs)
        sizes.append(len(states))
        return CountedStates(states)

    monkeypatch.setattr(material_query, "creative_states", counted)
    rows = query(session, sync_seed)
    assert len(rows) == 2
    assert all(row.coverage["status"] == "INCOMPLETE" for row in rows)
    # 页补全对全账户状态只允许固定次数遍历，不能随账户数或行数相乘。
    assert sum(visits) <= 3 * sum(sizes)


def test_unmapped_creatives_keep_money_as_unknown_rows(session, sync_seed):
    # 未映射创意单独成行，金额不丢，仍可排序。
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10"), material_row("p2", "20")],
    )
    rows = query(session, sync_seed, sort_by="spend", sort_direction="desc")
    assert [json.loads(r.row_key) for r in rows] == [
        ["unmapped_creative", "acc-1", "p2"],
        ["unmapped_creative", "acc-1", "p1"],
    ]
    for r in rows:
        assert r.coverage["reason"] == "creative_identity_unmapped"
        assert r.material_uses == ()
        assert r.display["account_id"] == "acc-1"
        assert r.metric_buckets[0].availability["revenue_d3"] == "PARTIAL"


def test_same_video_across_ads_and_accounts_is_one_row(session, sync_seed):
    # 同一视频在两个账户、三条广告各有帖子：素材维度一行，金额合计且不重复。
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10", "视频一"), material_row("p2", "5")],
    )
    publish(session, sync_seed, "acc-2", [material_row("p3", "7")])
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
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="b1",
        video="v1",
        ad_material="m3",
        post="p3",
    )
    rows = query(session, sync_seed)
    assert [json.loads(r.row_key) for r in rows] == [["material", "VIDEO", "v1"]]
    row = rows[0]
    assert row.metric_buckets[0].values["spend"] == Decimal(22)
    assert {
        (u.ad_ref.advertiser_id, u.ad_ref.remote_id) for u in row.material_uses
    } == {
        ("acc-1", "a1"),
        ("acc-1", "a2"),
        ("acc-2", "b1"),
    }
    assert row.display["material_name"] == "v1.mp4"
    assert row.display["account_id"] is None
    assert row.display["account_count"] == "2"


def test_directory_video_without_facts_and_link_by_ad_material(session, sync_seed):
    # 对照必须按广告内素材 ID 精确对上：同一广告里的另一个视频不能吃掉这笔钱。
    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
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
    rows = by_id(query(session, sync_seed))
    assert rows["v1"].metric_buckets[0].values["spend"] == Decimal(10)
    # acc-1 当天素材日报已完整，没有数据的视频填 0。
    assert rows["v2"].metric_buckets[0].values["spend"] == Decimal(0)
    assert [u.ad_material_id for u in rows["v2"].material_uses] == ["m2"]


def test_material_search_and_entity_filter_follow_usages(session, sync_seed):
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("p1", "10"), material_row("p2", "20")],
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
        video="v2",
        ad_material="m2",
        post="p2",
        adgroup="g2",
    )
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, query="广告a2")
    ] == ["v2"]
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, ids=("g1",))
    ] == ["v1"]
    # 帖子 ID 也能搜到所属视频。
    assert [
        r.display["main_material_id"] for r in query(session, sync_seed, ids=("p2",))
    ] == ["v2"]
    enabled = query(session, sync_seed, operation_statuses=("ENABLE",))
    assert {r.display["main_material_id"] for r in enabled} == {"v1", "v2"}
    assert query(session, sync_seed, operation_statuses=("DISABLE",)) == ()
    spend = query(session, sync_seed, min_spend=Decimal("15"))
    assert [r.display["main_material_id"] for r in spend] == ["v2"]


def test_duplicate_directory_row_without_ad_material_id_is_hidden(session, sync_seed):
    # 目录对同一广告同一视频另有一条不带广告内素材 ID 的记录，不应重复成为使用位置。
    publish(session, sync_seed, "acc-1", [material_row("p1", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a1",
            platform_material_id="v1",
            ad_material_id=None,
            material_type="VIDEO",
            name="",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    (row,) = query(session, sync_seed)
    assert [u.ad_material_id for u in row.material_uses] == ["m1"]


def test_default_material_rows_match_full_listing_for_videos_with_facts(
    session, sync_seed
):
    """默认口径只为有事实的帖子查视频对照；有数据的视频行与列出全部对象时一致。"""
    publish(session, sync_seed, "acc-1", [material_row("p1", "10", "视频一")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a1",
        video="v1",
        ad_material="m1",
        post="p1",
    )
    # 同一视频在另一账户的帖子当天没有事实：不贡献金额，使用位置仍归该视频行。
    use(
        session,
        sync_seed,
        adv="acc-2",
        ad="b1",
        video="v1",
        ad_material="m3",
        post="p9",
    )
    # 另一个只在目录里、当天没有数据的视频：默认不列出。
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="a3",
        video="v2",
        ad_material="m4",
        post="p8",
    )

    def rows(include_empty):
        return {
            r.row_key: (r.display, r.metric_buckets, r.material_uses)
            for r in query(session, sync_seed, include_empty=include_empty)
        }

    default, full = rows(False), rows(True)
    key = '["material", "VIDEO", "v1"]'
    assert list(default) == [key]
    assert (
        default[key][1][0].values["spend"]
        == full[key][1][0].values["spend"]
        == Decimal(10)
    )
    # 默认仅有创意事实的精确广告位置；include_empty 才加入无事实当前使用位置。
    assert len(default[key][2]) == 1
    assert len(full[key][2]) == 2


def test_material_grouping_keeps_exact_case_and_unicode_post_identities(
    session, sync_seed
):
    """身份按原始字节精确区分；名称的大小写/Unicode展示不能合并平台ID。"""
    posts = ["same", "SAME", "é", "e\u0301", "素材"]
    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row(post, str(index + 1)) for index, post in enumerate(posts)],
    )
    rows = query(session, sync_seed, include_empty=False)
    actual = {
        json.loads(row.row_key)[2]: row.metric_buckets[0].values["spend"]
        for row in rows
    }
    assert actual == {post: Decimal(index + 1) for index, post in enumerate(posts)}
