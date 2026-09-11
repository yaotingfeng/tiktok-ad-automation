"""只在独立测试数据库升级，检查历史归属/请求和精确远端 ID 不变。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TikTokConnection
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def previous_revision():
    backend = Path(__file__).resolve().parents[3]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app/alembic"))
    return ScriptDirectory.from_config(config).get_revision("mcp01").down_revision


def test_upgrade_preserves_api_bindings_ids_and_only_unambiguous_defaults(monkeypatch):
    with historical_database(monkeypatch, previous_revision()) as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            other = create_context(session)
            ids = [uuid4() for _ in range(3)]
            remote_id = "000123456789012345678901234567890"
            for index, identity in enumerate(ids):
                session.execute(
                    text(
                        "INSERT INTO tiktok_connection (id,tenant_id,status,credential_ciphertext,credential_version) VALUES (:id,:tenant,'ACTIVE',NULL,:revision)"
                    ),
                    {
                        "id": identity,
                        "tenant": context.tenant_id,
                        "revision": index + 3,
                    },
                )
            for bc in ("one", "two", "shared-api"):
                session.execute(
                    text(
                        "INSERT INTO tenant_bc (tenant_id,bc_id,name,ownership_conflict) VALUES (:tenant,:bc,'',false)"
                    ),
                    {"tenant": context.tenant_id, "bc": bc},
                )
            session.execute(
                text(
                    "INSERT INTO tenant_bc (tenant_id,bc_id,name,ownership_conflict) VALUES (:tenant,'one','',false)"
                ),
                {"tenant": other.tenant_id},
            )
            session.execute(
                text(
                    "INSERT INTO advertiser_account (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict) VALUES (:tenant,:id,'historic','USD','UTC','ENABLE',false)"
                ),
                {"tenant": context.tenant_id, "id": remote_id},
            )
            for bc, identity in (
                ("one", ids[0]),
                ("shared-api", ids[0]),
                ("two", ids[1]),
                ("two", ids[2]),
            ):
                session.execute(
                    text(
                        "INSERT INTO bc_account_access (tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,can_upload,can_build,permission_state) VALUES (:tenant,:bc,:advertiser,:connection,true,true,true,false,false,'UNKNOWN')"
                    ),
                    {
                        "tenant": context.tenant_id,
                        "bc": bc,
                        "advertiser": remote_id,
                        "connection": identity,
                    },
                )
            run_id = uuid4()
            session.execute(
                text("""INSERT INTO discovery_run
                (id,tenant_id,actor_id,connection_id,credential_version,status,work,revision,sent_count,created_at)
                VALUES (:id,:tenant,:actor,:connection,3,'COMPLETE',
                    CAST(:work AS json),0,0,now())"""),
                {
                    "id": run_id,
                    "tenant": context.tenant_id,
                    "actor": context.actor_id,
                    "connection": ids[0],
                    "work": json.dumps(
                        {"credential_version": 3, "remote_id": remote_id}
                    ),
                },
            )
            session.commit()

        command.upgrade(config, "mcp01")
        with Session(engine) as session:
            rows = session.execute(
                text("SELECT bc_id,connection_id FROM bc_default_route")
            ).all()
            assert set(rows) == {("one", ids[0]), ("shared-api", ids[0])}
            assert (
                session.execute(
                    text("SELECT count(*) FROM bc_connection_binding")
                ).scalar_one()
                == 4
            )
            assert (
                session.execute(
                    text("SELECT advertiser_id FROM advertiser_account")
                ).scalar_one()
                == remote_id
            )
            assert session.execute(
                text("SELECT work FROM discovery_run WHERE id=:id"), {"id": run_id}
            ).scalar_one() == {"credential_version": 3, "remote_id": remote_id}
            for index, identity in enumerate(ids):
                row = session.get(TikTokConnection, identity)
                assert (
                    row.kind,
                    row.credential_revision,
                    row.authorization_revision,
                ) == ("OFFICIAL_API", index + 3, 0)
            for table, old, new in (
                ("tiktok_connection", "credential_version", "credential_revision"),
                (
                    "authorization_attempt",
                    "base_credential_version",
                    "base_credential_revision",
                ),
                ("discovery_run", "credential_version", "credential_revision"),
                ("account_capability_job", "credential_version", "credential_revision"),
                ("build_scene_job", "credential_version", "credential_revision"),
            ):
                columns = {col["name"] for col in inspect(engine).get_columns(table)}
                assert old not in columns and new in columns
            provider_columns = {
                col["name"]
                for col in inspect(engine).get_columns("provider_connection")
            }
            assert "credential_version" in provider_columns
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    BCDefaultRoute(
                        tenant_id=other.tenant_id, bc_id="one", connection_id=ids[0]
                    )
                )
                session.flush()
            mcp = TikTokConnection(tenant_id=context.tenant_id, kind="OFFICIAL_MCP")
            session.add(mcp)
            session.flush()
            session.add(
                BCConnectionBinding(
                    tenant_id=context.tenant_id,
                    bc_id="one",
                    connection_id=mcp.id,
                    kind="OFFICIAL_MCP",
                )
            )
            session.flush()
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    BCConnectionBinding(
                        tenant_id=context.tenant_id,
                        bc_id="two",
                        connection_id=mcp.id,
                        kind="OFFICIAL_MCP",
                    )
                )
                session.flush()
            with pytest.raises(IntegrityError), session.begin_nested():
                mcp.authorization_revision = -1
                session.flush()

            session.commit()
        with pytest.raises(RuntimeError, match="while MCP connections exist"):
            command.downgrade(config, previous_revision())
        assert "kind" in {
            col["name"] for col in inspect(engine).get_columns("tiktok_connection")
        }
        # 测试库删除自己创建的 MCP fixture 后，API-only 数据允许返回旧 schema。
        with engine.begin() as db:
            db.execute(
                text("DELETE FROM bc_connection_binding WHERE kind='OFFICIAL_MCP'")
            )
            db.execute(text("DELETE FROM tiktok_connection WHERE kind='OFFICIAL_MCP'"))
        command.downgrade(config, previous_revision())
        columns = {
            col["name"] for col in inspect(engine).get_columns("tiktok_connection")
        }
        assert "credential_version" in columns and "kind" not in columns
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT advertiser_id FROM advertiser_account")
                ).scalar_one()
                == remote_id
            )
