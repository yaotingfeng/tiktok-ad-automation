"""新链接字段保留旧事实，有手动资料后拒绝破坏性降级。"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows, snapshot


def test_manual_link_migration_preserves_history_and_guards_downgrade(monkeypatch):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            historical_rows(session)
        command.upgrade(config, "automatic_mini_targets")
        with engine.connect() as connection:
            before = snapshot(connection)
        command.upgrade(config, "manual_promotion_links")
        with engine.connect() as connection:
            assert snapshot(connection) == before
            assert connection.execute(
                text("SELECT DISTINCT source FROM promotion_link")
            ).all() == [("provider",)]
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM draft_input WHERE manual_link <> '{}'::jsonb"
                    )
                ).scalar_one()
                == 0
            )
        command.downgrade(config, "automatic_mini_targets")
        with engine.connect() as connection:
            assert snapshot(connection) == before
        command.upgrade(config, "manual_promotion_links")
        with engine.begin() as connection:
            connection.execute(text("UPDATE promotion_link SET source='manual'"))
        with pytest.raises(RuntimeError, match="Manual link data exists"):
            command.downgrade(config, "automatic_mini_targets")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "manual_promotion_links"
            )
            assert connection.execute(
                text("SELECT DISTINCT source FROM promotion_link")
            ).all() == [("manual",)]
