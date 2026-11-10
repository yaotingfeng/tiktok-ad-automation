"""影子素材投影：金额守恒、身份缺口、差额方向与三源修订。"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import select

from app.modules.reporting import publish
from app.modules.reporting.read_models import models
from app.modules.reporting.read_models.dirty_buckets import ReadBucketKey, mark_dirty
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_creative_reporting import D, creative_row


def identity(session, seed, creative="mapped", *, assets=None):
    from app.modules.ads.creative_models import AdCreative

    assets = assets or [{"platform_material_id": "vid-1", "material_type": "VIDEO"}]
    item = AdCreative(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        creative_id=creative,
        ad_remote_id="ad-1",
        ad_material_id="am-1",
        assets=assets,
        material_type="VIDEO" if len(assets) == 1 else "CAROUSEL",
        platform_material_id=assets[0]["platform_material_id"]
        if len(assets) == 1
        else None,
        identity_status="RESOLVED" if len(assets) == 1 else "COMPOSITE",
        first_observed_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        published_version=1,
    )
    session.add(item)
    session.flush()
    return item


def build(session, seed, *, ad_spend="100", ad_revenue="30", rows=()):
    model = getattr(models, "CreativeMaterialEntityDay", None)
    assert model is not None, "separate creative projection model must exist"
    args = {
        "tenant_id": seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": publish.AccountMeta("USD", "Asia/Hong_Kong")},
        "start": D,
        "end": D,
    }
    publish.publish_ad_day(
        session, **args, rows=[ad_row("ad-1", D, spend=ad_spend, total=ad_revenue)]
    )
    publish.publish_creative_day(session, **args, rows=list(rows))
    return rebuild(session, seed, model)


def rebuild(session, seed, model):
    from app.modules.reporting.read_models.creative_projector import (
        project_creative_bucket,
    )

    key = ReadBucketKey(seed.tenant_id, "acc-1", "creative_material", D)
    revision = mark_dirty(session, [key])[key]
    assert project_creative_bucket(session, key, expected_version=revision).published
    session.expire_all()
    return session.exec(select(model).where(model.tenant_id == seed.tenant_id)).all()


def test_unmapped_creative_keeps_money_without_fake_reconciliation(session, sync_seed):
    identity(session, sync_seed)
    rows = build(
        session,
        sync_seed,
        rows=[creative_row("mapped", "90", "20"), creative_row("unknown", "10", "10")],
    )
    assert sum(r.spend for r in rows) == Decimal("100")
    unmapped = [r for r in rows if r.attribution_status == "UNMAPPED"]
    assert len(unmapped) == 1 and unmapped[0].spend == Decimal("10")
    # 这个用例的其他计数指标有独立差额；身份缺失不能制造消耗或总收入差额。
    assert all(r.spend == 0 and r.revenue_total == 0 for r in rows if r.attribution_status == "RESIDUAL")
    assert (
        session.connection()
        .execute(
            text("SELECT count(*) FROM material_entity_day WHERE tenant_id=:t"),
            {"t": sync_seed.tenant_id},
        )
        .scalar_one()
        == 0
    )


def test_revenue_only_difference_is_preserved_and_negative_spend_not_clamped(
    session, sync_seed
):
    identity(session, sync_seed)
    rows = build(session, sync_seed, rows=[creative_row("mapped", "101", "20")])
    residual = next(r for r in rows if r.attribution_status == "RESIDUAL")
    assert residual.spend == Decimal("-1") and residual.revenue_total == Decimal("10")
    assert sum(r.spend for r in rows) == Decimal("100")
    assert sum(r.revenue_total for r in rows) == Decimal("30")


def test_unknown_metric_does_not_become_known_residual(session, sync_seed):
    identity(session, sync_seed)
    rows = build(session, sync_seed, rows=[creative_row("mapped", "100", "-")])
    residual = next(r for r in rows if r.attribution_status == "RESIDUAL")
    assert residual.revenue_total is None and residual.revenue_total_has_null
    assert residual.source_status != "COMPLETE"


def test_multiframe_creative_is_one_economic_fact(session, sync_seed):
    identity(
        session,
        sync_seed,
        assets=[
            {"platform_material_id": "image-1", "material_type": "IMAGE"},
            {"platform_material_id": "image-2", "material_type": "IMAGE"},
        ],
    )
    rows = build(
        session,
        sync_seed,
        ad_spend="30",
        ad_revenue="10",
        rows=[creative_row("mapped", "30", "10")],
    )
    economic = [r for r in rows if r.attribution_status != "RESIDUAL"]
    assert len(economic) == 1 and economic[0].material_type == "CAROUSEL"
    assert (
        economic[0].spend == Decimal("30") and economic[0].platform_material_id is None
    )
    assert len(economic[0].assets) == 2


def test_identity_arriving_after_facts_marks_shadow_bucket_dirty(session, sync_seed):
    rows = build(session, sync_seed, rows=[creative_row("mapped", "100", "30")])
    assert rows[0].attribution_status == "UNMAPPED"
    key = (sync_seed.tenant_id, "acc-1", "creative_material", D)
    session.expire_all()
    old = session.get(models.ReadBucketState, key).source_revision
    identity(session, sync_seed)
    session.expire_all()
    state = session.get(models.ReadBucketState, key)
    assert (
        state.source_revision > old and state.source_revision > state.derived_revision
    )
    rows = rebuild(session, sync_seed, models.CreativeMaterialEntityDay)
    assert rows[0].attribution_status == "RESOLVED"
