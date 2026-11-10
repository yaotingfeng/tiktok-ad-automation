"""派生日桶重建的金额、空值、类型纠正与版本围栏。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import select

from tests.modules.reporting.test_material_query import material_row, publish, use


def test_derived_bucket_cannot_prevent_deleting_account_after_canonical_cleanup(
    session, sync_seed
):
    from sqlalchemy import delete

    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.ads.models import AdObject
    from app.modules.reporting.read_models.models import ReadBucketState

    account = AdvertiserAccount(
        tenant_id=sync_seed.tenant_id, advertiser_id="derived-cleanup", timezone="UTC"
    )
    session.add(account)
    session.flush()
    entity = AdObject(
        tenant_id=account.tenant_id,
        advertiser_id=account.advertiser_id,
        kind="ad",
        remote_id="removed",
        ad_type="REGULAR",
        observed_at=datetime.now(UTC),
        published_version=1,
    )
    session.add(entity)
    session.flush()
    # 清理程序可能先清派生元数据，之后才删除canonical行；源DELETE触发器
    # 会再次创建版本桶，但这个派生桶不能让最后一步账户删除失败。
    session.execute(
        delete(ReadBucketState).where(
            ReadBucketState.tenant_id == account.tenant_id,
            ReadBucketState.advertiser_id == account.advertiser_id,
        )
    )
    session.delete(entity)
    session.flush()
    session.delete(account)
    session.flush()
    assert not session.exec(
        select(ReadBucketState).where(
            ReadBucketState.tenant_id == sync_seed.tenant_id,
            ReadBucketState.advertiser_id == "derived-cleanup",
        )
    ).all()


def test_current_usage_keeps_ties_but_frozen_row_does_not_guess_actionable_id(
    session, sync_seed
):
    from app.modules.ads.models import AdMaterialReference
    from app.modules.reporting.read_models.models import CurrentAdMaterial
    from tests.modules.reporting.test_material_query import query

    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad",
        video="video",
        ad_material="old",
        post="post",
    )
    for inner in ["new-a", "new-b"]:
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                platform_material_id="video",
                ad_material_id=inner,
                material_type="VIDEO",
                complete=True,
                published_version=2,
            )
        )
    session.flush()
    projected = session.exec(
        select(CurrentAdMaterial).where(
            CurrentAdMaterial.tenant_id == sync_seed.tenant_id
        )
    ).all()
    assert {row.ad_material_id for row in projected} == {"new-a", "new-b"}
    row = query(session, sync_seed)[0]
    assert row.metric_buckets[0].values["spend"] == Decimal("10")
    assert row.material_uses == ()
