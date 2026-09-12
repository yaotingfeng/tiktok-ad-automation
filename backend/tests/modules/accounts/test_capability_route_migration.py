"""历史权限事实和 BC 独立分页的迁移保全；只操作临时测试库。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlmodel import Session

from app.modules.accounts.models import TenantBC, TikTokConnection
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def test_old_capability_facts_are_retained_without_inventing_authority(monkeypatch):
    with historical_database(monkeypatch, "mcp_stage_directory") as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            session.add_all(
                [connection, TenantBC(tenant_id=context.tenant_id, bc_id="bc")]
            )
            session.commit()
            connection_id = connection.id
        identity = uuid4()
        with engine.begin() as database:
            database.execute(
                text("""
                INSERT INTO account_capability_job
                (id,tenant_id,bc_id,connection_id,actor_id,credential_revision,directory_basis,
                 status,phase,next_page,seen_count,published_count,scope_known,scope_build,
                 scope_upload,revision,due_at,repair_after,created_at,failure_count)
                VALUES (:id,:tenant,'bc',:connection,:actor,5,:basis,'COMPLETE','DONE',3,
                        72,72,true,true,false,4,now(),now(),now(),0)
            """),
                {
                    "id": identity,
                    "tenant": context.tenant_id,
                    "connection": connection_id,
                    "actor": context.actor_id,
                    "basis": "f" * 64,
                },
            )
        command.upgrade(config, "mcp_capability_routes")
        with engine.connect() as database:
            row = (
                database.execute(
                    text("SELECT * FROM account_capability_job WHERE id=:id"),
                    {"id": identity},
                )
                .mappings()
                .one()
            )
            assert (row["status"], row["phase"], row["error_code"]) == (
                "STALE",
                "DONE",
                "capability_evidence_stale",
            )
            assert (
                row["credential_revision"],
                row["seen_count"],
                row["published_count"],
                row["revision"],
            ) == (5, 72, 72, 4)
            assert row["scope_known"] and row["scope_build"] and not row["scope_upload"]
            assert (
                row["channel"]
                is row["authorization_revision"]
                is row["adapter_contract_revision"]
                is None
            )
        with engine.begin() as database:
            database.execute(
                text(
                    "UPDATE account_capability_job SET channel='OFFICIAL_API', authorization_revision=0, adapter_contract_revision='official-api-v1' WHERE id=:id"
                ),
                {"id": identity},
            )
        with pytest.raises(
            RuntimeError, match="cannot discard frozen capability routes"
        ):
            command.downgrade(config, "mcp_stage_directory")


def test_directory_bc_scope_keeps_remote_pages_separate(monkeypatch):
    with historical_database(monkeypatch, "mcp_capability_routes") as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            session.add(connection)
            session.flush()
            run_id, connection_id = uuid4(), connection.id
            # 历史 schema 只播种当时已有的字段，避免当前 ORM 写入后续迁移列。
            session.execute(
                text("""
                INSERT INTO discovery_run
                (id,tenant_id,actor_id,connection_id,credential_revision,status,work,
                 revision,sent_count,created_at)
                VALUES (:id,:tenant,:actor,:connection,0,'RUNNING',
                        '{"bc_id":"bc-a"}'::json,0,0,now())
                """),
                {
                    "id": run_id,
                    "tenant": context.tenant_id,
                    "actor": context.actor_id,
                    "connection": connection_id,
                },
            )
            session.commit()
        with engine.begin() as database:
            database.execute(
                text("""
                INSERT INTO discovery_staged_page
                (tenant_id,connection_id,run_id,stage,page,total_pages,total_number,last_page,
                 pagination_kind,rows,call_evidence,schema_digest,observed_at)
                VALUES (:tenant,:connection,:run,'ASSETS',1,1,1,true,'REMOTE',
                        '[{"advertiser_id":"000900719925474099399999"}]'::jsonb,
                        '{"request_id":"actual-page-request"}'::jsonb,:digest,now())
            """),
                {
                    "tenant": context.tenant_id,
                    "connection": connection_id,
                    "run": run_id,
                    "digest": "a" * 64,
                },
            )
        command.upgrade(config, "mcp_directory_bc_scope")
        assert inspect(engine).get_pk_constraint("discovery_staged_page")[
            "constrained_columns"
        ] == ["run_id", "bc_id", "stage", "page"]
        with engine.begin() as database:
            old = (
                database.execute(text("SELECT * FROM discovery_staged_page"))
                .mappings()
                .one()
            )
            assert old["bc_id"] == "bc-a" and old["rows"] == [
                {"advertiser_id": "000900719925474099399999"}
            ]
            database.execute(
                text("""
                INSERT INTO discovery_staged_page
                SELECT tenant_id,connection_id,run_id,stage,page,total_pages,total_number,
                       last_page,pagination_kind,rows,call_evidence,schema_digest,observed_at,'bc-b'
                FROM discovery_staged_page WHERE run_id=:run
            """),
                {"run": run_id},
            )
            assert (
                database.execute(
                    text("SELECT count(*) FROM discovery_staged_page")
                ).scalar_one()
                == 2
            )
        with pytest.raises(
            RuntimeError, match="cannot discard staged directory BC scope"
        ):
            command.downgrade(config, "mcp_capability_routes")
