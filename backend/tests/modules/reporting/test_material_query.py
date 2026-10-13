import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.publish import AccountMeta, publish_material_day
from app.modules.reporting.schemas import ReportingFilter

D = date(2026, 10, 5)


def material_row(material_id, spend, adv="acc-1", name=None):
    return {
        "dimensions": {
            "advertiser_id": adv,
            "main_material_id": material_id,
            "main_material_type": "VIDEO",
            "stat_time_day": str(D),
        },
        "metrics": {
            "spend": spend,
            "main_material_name": name or material_id,
            "ad_revenue_value_d0": "2",
        },
    }


def seed_materials(session, seed):
    publish_material_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D,
        end=D,
        rows=[material_row("v1", "10", name="视频一"), material_row("v2", "20")],
    )


def query(session, seed, **kw):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(dimension="material", start_date=D, end_date=D, **kw),
    )


def test_material_money_survives_unknown_usage_and_sorts(session, sync_seed):
    seed_materials(session, sync_seed)
    rows = query(session, sync_seed, sort_by="spend", sort_direction="desc")
    assert [r.display["main_material_id"] for r in rows] == ["v2", "v1"]
    assert sum(r.metric_buckets[0].values["spend"] for r in rows) == Decimal(30)
    for r in rows:
        assert r.coverage["reason"] == "usage_unknown"
        assert r.material_uses == ()
        assert r.metric_buckets[0].availability["spend"] == "AVAILABLE"
        assert r.metric_buckets[0].availability["revenue_d3"] == "UNSUPPORTED"
        assert r.metric_buckets[0].availability["roas_d30"] == "UNSUPPORTED"
        assert json.loads(r.row_key) == [
            "acc-1",
            "material",
            r.display["main_material_id"],
            "VIDEO",
        ]


def test_material_usage_does_not_duplicate_money_and_is_account_scoped(
    session, sync_seed
):
    seed_materials(session, sync_seed)
    for adv, ad in (("acc-1", "a1"), ("acc-1", "a2"), ("acc-2", "foreign-ad")):
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                ad_remote_id=ad,
                platform_material_id="platform-v1",
                main_material_id="v1",
                main_material_type="VIDEO",
                material_type="VIDEO",
                complete=True,
                published_version=1,
            )
        )
    session.flush()
    row = next(
        r for r in query(session, sync_seed) if r.display["main_material_id"] == "v1"
    )
    assert {u.ad_ref.remote_id for u in row.material_uses} == {"a1", "a2"}
    assert row.metric_buckets[0].values["spend"] == Decimal(10)
    assert row.display["material_name"] == "视频一"
    assert row.coverage["status"] == "COMPLETE"


def test_material_ad_material_identity_and_incomplete_usage(session, sync_seed):
    seed_materials(session, sync_seed)
    for mid, complete in (("v1", True), ("v2", False)):
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id=f"a-{mid}",
                platform_material_id=f"p-{mid}",
                ad_material_id=mid,
                material_type="VIDEO",
                complete=complete,
                published_version=1,
            )
        )
    session.flush()
    rows = {r.display["main_material_id"]: r for r in query(session, sync_seed)}
    assert rows["v1"].material_uses[0].ad_material_id == "v1"
    assert rows["v2"].material_uses == ()


def _use(session, seed, ad, mid, adgroup="g1"):
    from datetime import UTC, datetime

    from app.modules.ads.models import AdObject

    now = datetime.now(UTC)
    session.add(
        AdMaterialReference(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id=ad,
            platform_material_id=f"p-{mid}",
            main_material_id=mid,
            main_material_type="VIDEO",
            material_type="VIDEO",
            complete=True,
            published_version=1,
        )
    )
    for kind, remote_id, parent, name, status in (
        ("ad", ad, adgroup, f"广告{ad}", "ENABLE"),
        ("adgroup", adgroup, "c1", f"组{adgroup}", "DISABLE"),
    ):
        if session.get(AdObject, (seed.tenant_id, "acc-1", kind, remote_id)) is None:
            session.add(
                AdObject(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
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


def test_material_search_matches_usage_hierarchy_and_entity_filter(session, sync_seed):
    # 搜索可命中使用位置的广告/广告组名称与 ID；目录筛选只保留命中的使用位置。
    seed_materials(session, sync_seed)
    _use(session, sync_seed, "a1", "v1")
    _use(session, sync_seed, "a2", "v2", adgroup="g2")
    by_ad = query(session, sync_seed, query="广告a2")
    assert [r.display["main_material_id"] for r in by_ad] == ["v2"]
    by_group_id = query(session, sync_seed, ids=("g1",))
    assert [r.display["main_material_id"] for r in by_group_id] == ["v1"]
    enabled = query(session, sync_seed, operation_statuses=("ENABLE",))
    assert {r.display["main_material_id"] for r in enabled} == {"v1", "v2"}
    assert query(session, sync_seed, operation_statuses=("DISABLE",)) == ()
    spend = query(session, sync_seed, min_spend=Decimal("15"))
    assert [r.display["main_material_id"] for r in spend] == ["v2"]
