"""真实 PG 验证旧列分段退休期间及删列后的目录发布合同。"""

from dataclasses import replace

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.integrations.tiktok.contracts.ads import AdMaterialUsage, MaterialUseRef
from app.modules.ads.creative_models import AdCreative
from app.modules.ads.models import AdMaterialReference
from app.modules.ads.sync import (
    _creative_material_stage,
    publish_directory,
    stage_directory_page,
)
from app.modules.builds.created_directory import publish_created_object
from app.modules.builds.execution_models import ExecutionStep
from tests.modules.ads.conftest import directory_seed as directory_seed
from tests.modules.ads.test_directory_sync import _authority, _run
from tests.modules.ads.test_material_directory_children import _source_page
from tests.modules.builds.test_created_directory import (
    complete_offline_identity_catalog as complete_offline_identity_catalog,
)
from tests.modules.builds.test_created_directory import (
    created_step as created_step,
)
from tests.modules.builds.test_created_directory import (
    executable as executable,
)


def _column_shape(session, retained):
    # 原生建表与已迁移到0117的库分别从无列/保留列开始；显式构造两种物理形态。
    exists = "creative_ids" in {
        column["name"]
        for column in inspect(session.connection()).get_columns("ad_material_reference")
    }
    if retained and not exists:
        session.execute(
            text("""ALTER TABLE ad_material_reference
            ADD COLUMN creative_ids jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(creative_ids)='array')""")
        )
    elif not retained and exists:
        session.execute(text("ALTER TABLE ad_material_reference DROP COLUMN creative_ids"))


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "dropped"])
def test_directory_keeps_shared_creative_members_and_carousel_order(
    session, directory_seed, monkeypatch, retained
):
    from app.modules.ads import sync

    _column_shape(session, retained)
    staged = _creative_material_stage(session)
    columns = {c["name"]: c for c in inspect(session.connection()).get_columns(staged)}
    assert not columns["creative_ids"]["nullable"]
    assert columns["creative_asset_ordinal"]["identity"]["always"]
    monkeypatch.setattr(sync, "_PUBLISH_BATCH_SIZE", 1)
    _authority(session, directory_seed)
    source = _source_page(directory_seed, 1, [("ad", "group")], terminal=True)
    owner = source.items[0]
    usages = tuple(
        AdMaterialUsage(
            MaterialUseRef(owner.ref, image, "images", "IMAGE"),
            None,
            "ENABLE",
            True,
            creative_ids=(cid,),
        )
        for image, cid in [("b", "carousel"), ("a", "carousel"), ("b", "shared")]
    )
    if retained:
        historical = AdMaterialReference(
            tenant_id=owner.ref.tenant_id,
            advertiser_id=owner.ref.advertiser_id,
            ad_remote_id=owner.ref.remote_id,
            platform_material_id="b",
            ad_material_id="images",
            material_type="IMAGE",
            complete=True,
            published_version=1,
        )
        session.add(historical)
        session.flush()
        session.execute(
            text("""UPDATE ad_material_reference SET creative_ids='["historical"]'::jsonb
            WHERE id=:id"""),
            {"id": historical.id},
        )
    run = _run(session, directory_seed, kind="ad", ad_type="SMART_PLUS")
    stage_directory_page(
        session,
        run_id=run.id,
        page=replace(source, materials=usages),
        claim_generation=1,
    )
    publish_directory(session, run_id=run.id, claim_generation=1)
    assert run.status == "COMPLETE"
    rows = {
        row.creative_id: row
        for row in session.exec(
            select(AdCreative).where(AdCreative.tenant_id == run.tenant_id)
        )
    }
    assert [asset["platform_material_id"] for asset in rows["carousel"].assets] == [
        "b",
        "a",
    ]
    assert rows["carousel"].identity_status == "COMPOSITE"
    assert rows["shared"].platform_material_id == "b"
    if retained:
        # 更新使用事实不能清掉尚待保全的旧 CID；新素材行使用数据库空数组默认值。
        legacy = dict(
            session.execute(
                text(
                    "SELECT platform_material_id,creative_ids FROM ad_material_reference"
                )
            ).all()
        )
        assert legacy == {"a": [], "b": ["historical"]}


@pytest.mark.parametrize("retained", [True, False], ids=["retained", "dropped"])
@pytest.mark.parametrize("created_step", ["native"], indirect=True)
def test_created_receipt_writes_current_identity_with_either_column_shape(
    created_step, retained
):
    engine, _, identity, route = created_step
    with Session(engine) as session, session.begin():
        _column_shape(session, retained)
        step = session.get(ExecutionStep, identity)
        publish_created_object(session, step=step, route=route)
        creative = session.get(
            AdCreative, (step.tenant_id, "account-A", "created-native")
        )
        assert creative.platform_material_id == "target-0"
        assert creative.ad_material_id == "created-mid"
        material = session.exec(select(AdMaterialReference)).one()
        assert material.platform_material_id == "target-0"
        if retained:
            assert (
                session.execute(
                    text("SELECT creative_ids FROM ad_material_reference WHERE id=:id"),
                    {"id": material.id},
                ).scalar_one()
                == []
            )
