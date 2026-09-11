"""升级保留目录与历史观察，只让实际权限或成员变化推进语义围栏。"""

from datetime import UTC, datetime

from alembic import command
from sqlalchemy import text
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


def test_upgrade_preserves_history_and_separates_observation_from_authority(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp02") as (engine, config):
        with Session(engine) as session, session.begin():
            context = create_context(session)
            connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            session.add(connection)
            session.add(TenantBC(tenant_id=context.tenant_id, bc_id="observed-bc"))
            session.add(
                AdvertiserAccount(
                    tenant_id=context.tenant_id,
                    advertiser_id="observed-account",
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
            )
            session.flush()
            grant = BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id="observed-bc",
                advertiser_id="observed-account",
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
            )
            run = DiscoveryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=connection.id,
                status="COMPLETE",
            )
            session.add_all([grant, run])
            session.flush()
            tenant_id, connection_id, run_id = context.tenant_id, connection.id, run.id
        queries = {
            table: text(f"SELECT row_to_json(r)::text FROM {table} r ORDER BY 1")
            for table in (
                "bc_account_access",
                "discovery_run",
                "account_directory_revision",
            )
        }
        with engine.connect() as db:
            before = {
                table: db.execute(query).all() for table, query in queries.items()
            }
        command.upgrade(config, "mcp_directory_semantics")
        with engine.begin() as db:
            assert {
                table: db.execute(query).all() for table, query in queries.items()
            } == before
            params = {
                "tenant": tenant_id,
                "connection": connection_id,
                "run": run_id,
                "checked": datetime.now(UTC),
            }
            revision_query = text(
                "SELECT revision FROM account_directory_revision WHERE tenant_id=:tenant AND connection_id=:connection"
            )
            revision = db.execute(revision_query, params).scalar_one()
            db.execute(
                text(
                    "UPDATE bc_account_access SET last_seen_run_id=:run,checked_at=:checked WHERE tenant_id=:tenant AND connection_id=:connection"
                ),
                params,
            )
            assert db.execute(revision_query, params).scalar_one() == revision
            db.execute(
                text(
                    "UPDATE bc_account_access SET authorized=false WHERE tenant_id=:tenant AND connection_id=:connection"
                ),
                params,
            )
            assert db.execute(revision_query, params).scalar_one() == revision + 1
            db.execute(
                text(
                    "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
                ),
                params,
            )
            assert db.execute(revision_query, params).scalar_one() == revision + 2
