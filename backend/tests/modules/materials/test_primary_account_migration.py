"""主素材账户升级只增加空选择，不搬动已上传/在途素材的实际来源。"""

from alembic import command
from sqlalchemy import MetaData, Table, inspect, text
from sqlmodel import Session

from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.migration_database import (
    historical_database,
    insert_historical_bc,
    insert_historical_material,
)
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import mapping


def test_primary_account_upgrade_preserves_distributed_source_history(monkeypatch):
    with historical_database(monkeypatch, "preview_display_drama_id") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            insert_historical_bc(db, tenant_id=context.tenant_id, bc_id="old-bc")
            operations = Table(
                "material_asset_operation", MetaData(), autoload_with=db.connection()
            )
            for index, status in enumerate(("succeeded", "result_unknown")):
                file = MaterialFile(
                    tenant_id=context.tenant_id,
                    bc_id="old-bc",
                    file_name=f"old-{index}.mp4",
                    object_key=f"synthetic/old-{index}",
                    byte_size=100,
                )
                insert_historical_material(db, file)
                db.flush()
                asset = mapping(
                    db, context, file, account=f"actual-source-{index}", historical=True
                )
                operation = MaterialAssetOperation(
                    tenant_id=context.tenant_id,
                    bc_id=file.bc_id,
                    material_id=file.id,
                    advertiser_id=asset.advertiser_id,
                    path="upload_original",
                    status=status,
                    request_digest=str(index) * 64,
                    remote_response={"video_id": asset.video_id},
                )
                # 主账户迁移早于素材替代链，播种时排除后续新增列。
                db.execute(
                    operations.insert().values(
                        **{
                            key: value
                            for key, value in operation.model_dump().items()
                            if key in operations.c and value is not None
                        }
                    )
                )
        tables = ("account_material", "material_asset_operation", "material_file")
        with engine.connect() as db:
            before = {
                name: db.execute(text(f"SELECT to_jsonb(t) FROM {name} t ORDER BY id"))
                .scalars()
                .all()
                for name in tables
            }
            bc_before = db.execute(
                text("SELECT to_jsonb(t) FROM tenant_bc t")
            ).scalar_one()
        command.upgrade(config, "material_primary_account")
        column = next(
            item
            for item in inspect(engine).get_columns("tenant_bc")
            if item["name"] == "material_advertiser_id"
        )
        assert column["nullable"] is True
        with engine.connect() as db:
            for name in tables:
                assert (
                    db.execute(text(f"SELECT to_jsonb(t) FROM {name} t ORDER BY id"))
                    .scalars()
                    .all()
                    == before[name]
                )
            bc_after = db.execute(
                text("SELECT to_jsonb(t) FROM tenant_bc t")
            ).scalar_one()
            assert bc_after == {**bc_before, "material_advertiser_id": None}
