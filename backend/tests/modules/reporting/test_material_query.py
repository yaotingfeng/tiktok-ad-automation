import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.material_query import build_material_rows
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
    return build_material_rows(
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
