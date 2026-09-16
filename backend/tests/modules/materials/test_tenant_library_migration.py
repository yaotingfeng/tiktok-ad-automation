"""租户素材可跨 BC 消费，真实账户位置和原上传来源仍受数据库约束。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.materials.test_tenant_materials import mapping, material


def test_same_material_has_independent_actual_bc_mappings(session, context):
    file = material(session, context, "Moon.mp4")
    original = mapping(session, context, file)
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-b"))
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id="bc-b",
            advertiser_id=original.advertiser_id,
            connection_id=original.connection_id,
            in_bc=True,
            authorized=True,
            active=True,
        )
    )
    session.flush()
    copy = AccountMaterial(
        tenant_id=context.tenant_id,
        bc_id="bc-b",
        material_id=file.id,
        advertiser_id=original.advertiser_id,
        connection_id=original.connection_id,
        video_id="actual-b-video",
        status="available",
        verified_at=datetime.now(UTC),
    )
    session.add(copy)
    session.flush()
    assert session.get(MaterialFile, file.id).bc_id == "bc-a"
    assert original.video_id != copy.video_id


def test_consumer_foreign_keys_keep_tenant_and_original_upload_keeps_bc(session):
    inspector = inspect(session.connection())
    for table in (
        "account_material",
        "material_asset_operation",
        "material_distribution",
        "material_cover_job",
        "draft_group_material",
        "preview_group_material",
        "execution_step",
    ):
        keys = [
            fk
            for fk in inspector.get_foreign_keys(table)
            if fk["referred_table"] == "material_file"
        ]
        assert [fk["constrained_columns"] for fk in keys] == [
            ["tenant_id", "material_id"]
        ]
    for table in (
        "object_upload",
        "material_upload_attempt",
        "temporary_material_object",
        "ingest_session_file",
    ):
        keys = [
            fk
            for fk in inspector.get_foreign_keys(table)
            if fk["referred_table"] == "material_file"
        ]
        assert [fk["constrained_columns"] for fk in keys] == [
            ["tenant_id", "bc_id", "material_id"]
        ]


def test_cross_tenant_material_reference_rejected(session, context, other_context):
    file = material(session, context, "Moon.mp4")
    foreign = material(session, other_context, "Other.mp4")
    original = mapping(session, context, file)
    with pytest.raises(IntegrityError), session.begin_nested():
        original.material_id = foreign.id
        session.add(original)
        session.flush()


def test_operation_cannot_name_a_bc_outside_its_tenant(session, context):
    from app.modules.materials.models import MaterialAssetOperation

    file = material(session, context, "Moon.mp4")
    original = mapping(session, context, file)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            MaterialAssetOperation(
                tenant_id=context.tenant_id,
                bc_id="unregistered-bc",
                material_id=file.id,
                advertiser_id=original.advertiser_id,
                path="share_source",
                request_digest="a" * 64,
            )
        )
        session.flush()


def test_historical_upgrade_preserves_rows_and_refuses_cross_bc_downgrade(monkeypatch):
    from alembic import command
    from sqlalchemy import text
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.conftest import create_context

    with historical_database(monkeypatch, "material_share_receipts") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            owner = create_context(db)
            file = material(db, owner, "historical.mp4")
            asset = mapping(db, owner, file)
            before = {
                table: db.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")
                )
                .scalars()
                .all()
                for table in ("material_file", "account_material")
            }
            file_id, advertiser, connection = (
                file.id,
                asset.advertiser_id,
                asset.connection_id,
            )
        command.upgrade(config, "tenant_material_library")
        with Session(engine) as db, db.begin():
            for table, values in before.items():
                assert (
                    db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id"))
                    .scalars()
                    .all()
                    == values
                )
            db.add(TenantBC(tenant_id=owner.tenant_id, bc_id="bc-b"))
            db.flush()
            db.add(
                BCAccountAccess(
                    tenant_id=owner.tenant_id,
                    bc_id="bc-b",
                    advertiser_id=advertiser,
                    connection_id=connection,
                    in_bc=True,
                    authorized=True,
                    active=True,
                )
            )
            db.flush()
            db.add(
                AccountMaterial(
                    tenant_id=owner.tenant_id,
                    bc_id="bc-b",
                    material_id=file_id,
                    advertiser_id=advertiser,
                    connection_id=connection,
                    video_id="copy-b",
                    status="available",
                    verified_at=datetime.now(UTC),
                )
            )
        with pytest.raises(RuntimeError, match="cross-BC"):
            command.downgrade(config, "material_share_receipts")


def test_seed_upgrade_starts_empty_and_preserves_previous_distribution(monkeypatch):
    from uuid import uuid4

    from alembic import command
    from sqlalchemy import text
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.conftest import create_context

    with historical_database(monkeypatch, "tenant_material_library") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            owner = create_context(db)
            file = material(db, owner, "before-seed.mp4")
            asset = mapping(db, owner, file)
            identity = uuid4()
            db.execute(
                text(
                    "INSERT INTO material_distribution (id,tenant_id,bc_id,material_id,advertiser_id,actor_id,path,status) "
                    "VALUES (:id,:tenant,:bc,:material,:account,:actor,'existing_target','ready')"
                ),
                {
                    "id": identity,
                    "tenant": owner.tenant_id,
                    "bc": file.bc_id,
                    "material": file.id,
                    "account": asset.advertiser_id,
                    "actor": owner.actor_id,
                },
            )
            before = db.execute(
                text("SELECT to_jsonb(d) FROM material_distribution d WHERE id=:id"),
                {"id": identity},
            ).scalar_one()
        command.upgrade(config, "material_bc_seed")
        with Session(engine) as db:
            assert (
                db.execute(text("SELECT count(*) FROM material_bc_seed")).scalar_one()
                == 0
            )
            after = db.execute(
                text("SELECT to_jsonb(d) FROM material_distribution d WHERE id=:id"),
                {"id": identity},
            ).scalar_one()
            assert after == {**before, "seed_id": None}
        command.downgrade(config, "tenant_material_library")
        with Session(engine) as db:
            assert (
                db.execute(
                    text(
                        "SELECT to_jsonb(d) FROM material_distribution d WHERE id=:id"
                    ),
                    {"id": identity},
                ).scalar_one()
                == before
            )
