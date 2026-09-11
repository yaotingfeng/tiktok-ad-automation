from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlmodel import Session

from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def test_staging_migration_preserves_live_directory_and_refuses_evidence_loss(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp01") as (engine, config):
        with Session(engine) as session:
            context = create_context(session)
            connection = TikTokConnection(
                tenant_id=context.tenant_id,
                kind="OFFICIAL_API",
                status="ACTIVE",
                credential_revision=7,
            )
            bc = TenantBC(tenant_id=context.tenant_id, bc_id="historical-bc")
            account = AdvertiserAccount(
                tenant_id=context.tenant_id,
                advertiser_id="0009007199254740993101234567",
                name="Existing live account",
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            )
            session.add_all([connection, bc, account])
            session.flush()
            run = DiscoveryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=connection.id,
                credential_revision=7,
                status="COMPLETE",
                work={
                    "stage": "FINALIZE",
                    "immutable_remote_id": account.advertiser_id,
                },
            )
            session.add(run)
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    advertiser_id=account.advertiser_id,
                    connection_id=connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_build=True,
                    permission_state="VERIFIED",
                    last_seen_run_id=run.id,
                )
            )
            session.commit()
            run_id, connection_id = run.id, connection.id

        def live_snapshot():
            with engine.connect() as database:
                return tuple(
                    database.execute(
                        text(f"SELECT row_to_json(t) FROM {table} t")
                    ).scalar_one()
                    for table in (
                        "tiktok_connection",
                        "advertiser_account",
                        "bc_account_access",
                        "discovery_run",
                    )
                )

        original = live_snapshot()
        command.upgrade(config, "mcp_stage_directory")
        assert live_snapshot() == original
        assert "discovery_staged_page" in inspect(engine).get_table_names()
        # 历史版本按当时列显式插入，后续 ORM 新字段不能写入旧 schema。
        with engine.begin() as database:
            database.execute(
                text("""
                INSERT INTO discovery_staged_page
                (tenant_id,connection_id,run_id,stage,page,total_pages,total_number,last_page,
                 pagination_kind,rows,call_evidence,schema_digest,observed_at)
                VALUES (:tenant,:connection,:run,'ASSETS',1,1,1,true,'REMOTE',
                        '[{"advertiser_id":"new-staged-account"}]'::jsonb,
                        '{"request_id":"synthetic-request"}'::jsonb,:digest,:now)
            """),
                {
                    "tenant": context.tenant_id,
                    "connection": connection_id,
                    "run": run_id,
                    "digest": "a" * 64,
                    "now": datetime.now(UTC),
                },
            )
        assert live_snapshot() == original
        with pytest.raises(RuntimeError, match="staging evidence exists"):
            command.downgrade(config, "mcp01")
        assert "discovery_staged_page" in inspect(engine).get_table_names()
        assert live_snapshot() == original
        # 仅清除独立临时迁移数据库内本例的合成暂存页，验证无证据时允许回退。
        with engine.begin() as database:
            database.execute(
                text("DELETE FROM discovery_staged_page WHERE run_id=:id"),
                {"id": run_id},
            )
        command.downgrade(config, "mcp01")
        assert "discovery_staged_page" not in inspect(engine).get_table_names()
        assert live_snapshot() == original
        command.upgrade(config, "mcp_stage_directory")
        assert live_snapshot() == original
