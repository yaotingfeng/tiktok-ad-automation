"""真实历史数据库补齐显示编号，不改原有身份或链接。"""

from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows


def test_display_id_migration_backfills_only_numeric_attribution(monkeypatch):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            historical_rows(session)
        command.upgrade(config, "manual_promotion_links")
        with engine.begin() as connection:
            connection.execute(text("UPDATE provider_connection SET kind='wangyan'"))
            connection.execute(
                text(
                    "UPDATE provider_drama SET external_drama_id='6a98f85eadb6903f924e6950'"
                )
            )
            connection.execute(
                text(
                    "UPDATE promotion_link SET attribution=jsonb_build_object('drama_int_id', 31091)"
                )
            )
        command.upgrade(config, "provider_display_drama_id")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT external_drama_id, display_drama_id FROM provider_drama")
            ).one() == ("6a98f85eadb6903f924e6950", "31091")
        command.downgrade(config, "manual_promotion_links")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE promotion_link SET attribution=jsonb_build_object('drama_int_id', true)"
                )
            )
        command.upgrade(config, "provider_display_drama_id")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT display_drama_id FROM provider_drama")
                ).scalar_one()
                is None
            )
