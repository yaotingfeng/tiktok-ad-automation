"""物理退休必须先保全官方身份，当前 schema 不再重建旧表。"""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.read_models.ddl import trigger_statements


def test_current_schema_has_no_retired_tables_or_creative_array(session):
    retired = {"material_report_day", "material_post_link", "material_link_scan"}
    assert not retired.intersection(inspect(session.connection()).get_table_names())
    assert not retired.intersection(SQLModel.metadata.tables)
    assert "creative_ids" not in AdMaterialReference.__table__.c
    assert "creative_ids" not in {
        c["name"]
        for c in inspect(session.connection()).get_columns("ad_material_reference")
    }
    assert not any(name in "\n".join(trigger_statements()) for name in retired)
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM pg_proc WHERE proname LIKE 'qrm_material_post_link_%' OR proname LIKE 'qrm_material_report_day_%'"
            )
        ).scalar_one()
        == 0
    )


def test_migration_blocks_missing_identity_then_preserves_bounded_backfill(monkeypatch):
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    import pytest
    from alembic import command
    from sqlmodel import Session

    from app.modules.ads.creatives import upsert_creatives
    from scripts.preserve_legacy_creative_identities import preserve_creative_identities
    from tests.migration_database import historical_database
    from tests.modules.reporting.sync_conftest import sync_seed

    with historical_database(monkeypatch, "0114_retire_material") as (engine, config):
        with Session(engine) as db, db.begin():
            seed = sync_seed.__wrapped__(db)
            tenant = seed.tenant_id
            # 同一创意两张图不能因父广告分页而拆分；另账户相同 CID 必须隔离。
            rows = [
                ("acc-1", "a-parent", "a-image", "carousel", "IMAGE"),
                ("acc-1", "a-parent", "b-image", "carousel", "IMAGE"),
                ("acc-1", "z-orphan", "video", "orphan", "VIDEO"),
                ("acc-2", "other", "other-video", "orphan", "VIDEO"),
            ]
            for account, parent, asset, cid, kind in rows:
                db.execute(
                    text("""INSERT INTO ad_material_reference
                    (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
                     ad_material_id,material_type,name,creative_ids,complete,published_version)
                    VALUES (:id,:tenant,:account,:parent,:asset,'mid',:kind,'legacy',
                            CAST(:ids AS jsonb),true,1)"""),
                    {
                        "id": uuid4(),
                        "tenant": tenant,
                        "account": account,
                        "parent": parent,
                        "asset": asset,
                        "kind": kind,
                        "ids": json.dumps([cid]),
                    },
                )
            # 已有有序轮播是权威完整事实，旧引用没有位置列不能改变顺序。
            assets = [
                {"platform_material_id": p, "material_type": "IMAGE"}
                for p in ["b-image", "a-image"]
            ]
            upsert_creatives(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                observed_at=datetime.now(UTC),
                published_version=5,
                rows=[
                    {
                        "creative_id": "carousel",
                        "ad_remote_id": "a-parent",
                        "ad_material_id": "mid",
                        "assets": assets,
                        "operation_status": "DISABLE",
                    }
                ],
            )
        with pytest.raises(RuntimeError, match="not preserved"):
            command.upgrade(config, "0115_drop_legacy_material")
        assert inspect(engine).has_table("material_post_link")
        with Session(engine) as db, db.begin():
            first = preserve_creative_identities(
                db, tenant_id=tenant, advertiser_id="acc-1", parent_limit=1
            )
            assert first["after_parent"] == "a-parent" and not first["done"]
            assert (
                db.execute(
                    text("SELECT count(*) FROM ad_creative WHERE creative_id='orphan'")
                ).scalar_one()
                == 0
            )
        with Session(engine) as db, db.begin():
            second = preserve_creative_identities(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                parent_limit=1,
                after_parent=first["after_parent"],
            )
            assert second["after_parent"] == "z-orphan"
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-2")
            # 相同批次重试不损失身份、不提升版本或改变轮播顺序。
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
        command.upgrade(config, "0115_drop_legacy_material")
        with engine.connect() as connection:
            actual = connection.execute(
                text(
                    "SELECT advertiser_id,creative_id,ad_remote_id,assets,identity_status,published_version,operation_status FROM ad_creative ORDER BY advertiser_id,creative_id"
                )
            ).all()
            assert len(actual) == 3
            assert actual[0][1:] == (
                "carousel",
                "a-parent",
                assets,
                "COMPOSITE",
                5,
                "DISABLE",
            )
            assert actual[1][1:3] == ("orphan", "z-orphan")
            assert actual[2][1:3] == ("orphan", "other")
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0115_drop_legacy_material"
            )
        assert not inspect(engine).has_table("material_report_day")
        command.check(config)
        with pytest.raises(RuntimeError, match="irreversible"):
            command.downgrade(config, "0114_retire_material")


def test_existing_unknown_and_conflict_must_preserve_legacy_relationships(monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    import pytest
    from alembic import command
    from sqlmodel import Session

    from app.modules.ads.creatives import upsert_creatives
    from scripts.preserve_legacy_creative_identities import preserve_creative_identities
    from tests.migration_database import historical_database
    from tests.modules.reporting.sync_conftest import sync_seed

    with historical_database(monkeypatch, "0114_retire_material") as (engine, config):
        now = datetime.now(UTC)
        with Session(engine) as db, db.begin():
            seed = sync_seed.__wrapped__(db)
            tenant = seed.tenant_id
            for cid in ("unknown", "conflict", "missing-mid"):
                db.execute(
                    text("""INSERT INTO ad_material_reference
                    (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
                     ad_material_id,material_type,name,creative_ids,complete,published_version)
                    VALUES (:id,:tenant,'acc-1',CAST(:cid AS text),'legacy-video','legacy-mid','VIDEO',
                            'legacy',jsonb_build_array(CAST(:cid AS text)),true,1)"""),
                    {"id": uuid4(), "tenant": tenant, "cid": cid},
                )
                assets = (
                    []
                    if cid == "unknown"
                    else [
                        {
                            "material_type": "VIDEO",
                            "platform_material_id": "other-video"
                            if cid == "conflict"
                            else "legacy-video",
                        }
                    ]
                )
                upsert_creatives(
                    db,
                    tenant_id=tenant,
                    advertiser_id="acc-1",
                    observed_at=now,
                    published_version=5,
                    rows=[
                        {
                            "creative_id": cid,
                            "ad_remote_id": cid,
                            "assets": assets,
                            "operation_status": "DISABLE",
                        }
                    ],
                )
            db.execute(
                text(
                    "UPDATE ad_creative SET identity_status='CONFLICT' WHERE creative_id='conflict'"
                )
            )
        # 三个CID都存在，但空身份、缺MID和冲突行均不能直接视作已保全。
        with pytest.raises(RuntimeError, match="not preserved"):
            command.upgrade(config, "0115_drop_legacy_material")
        with Session(engine) as db, db.begin():
            preserve_creative_identities(db, tenant_id=tenant, advertiser_id="acc-1")
            rows = {
                r["creative_id"]: r
                for r in db.execute(text("SELECT * FROM ad_creative")).mappings()
            }
            assert rows["unknown"]["identity_status"] == "RESOLVED"
            assert rows["unknown"]["platform_material_id"] == "legacy-video"
            assert rows["unknown"]["published_version"] == 5
            assert rows["unknown"]["observed_at"] == now
            assert rows["unknown"]["operation_status"] == "DISABLE"
            assert rows["missing-mid"]["ad_material_id"] == "legacy-mid"
            conflict = rows["conflict"]
            assert conflict["identity_status"] == "CONFLICT"
            assert conflict["platform_material_id"] == "other-video"
            assert conflict["evidence"]["legacy_identities"][0]["identity"][
                "assets"
            ] == [{"material_type": "VIDEO", "platform_material_id": "legacy-video"}]
            assert (
                preserve_creative_identities(
                    db, tenant_id=tenant, advertiser_id="acc-1"
                )["written"]
                == 0
            )
        command.upgrade(config, "0115_drop_legacy_material")
        assert not inspect(engine).has_table("material_report_day")


def test_legacy_conflict_across_parent_pages_survives_future_weak_mid_observation(
    monkeypatch,
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from alembic import command
    from sqlmodel import Session

    from app.modules.ads.creatives import upsert_creatives
    from scripts.preserve_legacy_creative_identities import preserve_creative_identities
    from tests.migration_database import historical_database
    from tests.modules.reporting.sync_conftest import sync_seed

    with historical_database(monkeypatch, "0114_retire_material") as (engine, config):
        with Session(engine) as db, db.begin():
            seed = sync_seed.__wrapped__(db)
            tenant = seed.tenant_id
            for parent in ("a-parent", "b-parent"):
                db.execute(
                    text("""INSERT INTO ad_material_reference
                    (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
                     material_type,name,creative_ids,complete,published_version)
                    VALUES (:id,:tenant,'acc-1',:parent,'video','VIDEO','legacy',
                            '["same-cid"]'::jsonb,true,1)"""),
                    {"id": uuid4(), "tenant": tenant, "parent": parent},
                )
        with Session(engine) as db, db.begin():
            page = preserve_creative_identities(
                db, tenant_id=tenant, advertiser_id="acc-1", parent_limit=1
            )
            assert page["after_parent"] == "a-parent"
        with Session(engine) as db, db.begin():
            preserve_creative_identities(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                parent_limit=1,
                after_parent=page["after_parent"],
            )
            conflict = (
                db.execute(
                    text("SELECT * FROM ad_creative WHERE creative_id='same-cid'")
                )
                .mappings()
                .one()
            )
            assert conflict["identity_status"] == "CONFLICT"
            assert conflict["ad_remote_id"] == "a-parent"
            assert (
                conflict["evidence"]["conflicts"][0]["identity"]["ad_remote_id"]
                == "b-parent"
            )
            assert conflict["evidence"]["conflicts"][0]["published_version"] == 1
            upsert_creatives(
                db,
                tenant_id=tenant,
                advertiser_id="acc-1",
                observed_at=datetime.now(UTC),
                published_version=10,
                rows=[
                    {
                        "creative_id": "same-cid",
                        "ad_remote_id": "a-parent",
                        "ad_material_id": "new-mid",
                        "assets": [
                            {"material_type": "VIDEO", "platform_material_id": "video"}
                        ],
                    }
                ],
            )
            assert (
                db.execute(
                    text(
                        "SELECT identity_status FROM ad_creative WHERE creative_id='same-cid'"
                    )
                ).scalar_one()
                == "CONFLICT"
            )
        command.upgrade(config, "0115_drop_legacy_material")
