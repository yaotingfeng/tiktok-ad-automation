"""新建独立历史测试库验证升级/拒绝破坏性降级，不降级业务库。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.materials.push_models import MaterialPushBatch
from tests.migration_database import historical_database, insert_historical_bc
from tests.modules.conftest import create_context


def test_upgrade_keeps_tenants_and_default_bc_is_scoped(monkeypatch):
    with historical_database(monkeypatch, "audience_targeting") as (engine, config):
        with Session(engine) as db, db.begin():
            first, second = create_context(db), create_context(db)
            insert_historical_bc(db, tenant_id=first.tenant_id, bc_id="own")
            insert_historical_bc(db, tenant_id=second.tenant_id, bc_id="foreign")
            before = db.execute(
                text("SELECT id,name,active FROM tenant ORDER BY id")
            ).all()
        command.upgrade(config, "material_push")
        with engine.begin() as db:
            assert (
                db.execute(text("SELECT id,name,active FROM tenant ORDER BY id")).all()
                == before
            )
            assert db.execute(
                text("SELECT default_bc_id FROM tenant")
            ).scalars().all() == [None, None]
            with pytest.raises(IntegrityError), db.begin_nested():
                db.execute(
                    text("UPDATE tenant SET default_bc_id='foreign' WHERE id=:id"),
                    {"id": first.tenant_id},
                )
            db.execute(
                text("UPDATE tenant SET default_bc_id='own' WHERE id=:id"),
                {"id": first.tenant_id},
            )
        with pytest.raises(RuntimeError, match="默认 BC"):
            command.downgrade(config, "audience_targeting")
        with engine.begin() as db:
            db.execute(text("UPDATE tenant SET default_bc_id=NULL"))
        command.downgrade(config, "audience_targeting")
        assert "material_push_batch" not in inspect(engine).get_table_names()
        command.upgrade(config, "material_push")
        with Session(engine) as db, db.begin():
            db.add(
                MaterialPushBatch(
                    tenant_id=first.tenant_id,
                    tenant_name=before[0].name,
                    bc_id="own",
                    actor_id=first.actor_id,
                    key_id="offline-test",
                    request_id=uuid4(),
                    request_digest="0" * 64,
                    frozen_route={
                        "tenant_id": str(first.tenant_id),
                        "bc_id": "own",
                        "connection_id": str(uuid4()),
                        "channel": "OFFICIAL_API",
                        "authorization_revision": 1,
                        "adapter_contract_revision": "official-api-v1",
                        "binding_revision": 0,
                    },
                )
            )
        with pytest.raises(RuntimeError, match="已有外部素材批次"):
            command.downgrade(config, "audience_targeting")
        # 历史行为断言结束后再校验当前模型，后续迁移不会使旧版本误报落后。
        command.upgrade(config, "head")
        command.check(config)


def test_duplicate_names_stop_upgrade_without_renaming(monkeypatch):
    with historical_database(monkeypatch, "audience_targeting") as (engine, config):
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO tenant (id,name,active) VALUES (:id,'重名',true)"),
                [{"id": uuid4()}, {"id": uuid4()}],
            )
        with pytest.raises(RuntimeError, match="重名租户"):
            command.upgrade(config, "material_push")
        with engine.connect() as db:
            assert db.execute(text("SELECT name FROM tenant")).scalars().all() == [
                "重名",
                "重名",
            ]
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "audience_targeting"
            )
        assert "material_push_batch" not in inspect(engine).get_table_names()
