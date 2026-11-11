"""真实0114历史上升级；不降级共享库，不改写旧冻结广告意图。"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows


def test_copy_source_migration_preserves_frozen_history_and_normal_references(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as session, session.begin():
            historical_rows(session)
        command.upgrade(config, "0114_retire_material")
        queries = {
            "draft": "SELECT to_jsonb(d) FROM build_draft d ORDER BY id",
            "preview": "SELECT to_jsonb(p) FROM build_preview p ORDER BY id",
            "drama": "SELECT to_jsonb(d)-'provider_drama_id' FROM preview_drama d ORDER BY drama_id",
            "steps": "SELECT to_jsonb(s) FROM execution_step s ORDER BY id",
        }
        with engine.connect() as db:
            original = {
                key: db.execute(text(query)).all() for key, query in queries.items()
            }
        command.upgrade(config, "0115_copy_current_source")
        with engine.connect() as db:
            assert {
                key: db.execute(text(query)).all() for key, query in queries.items()
            } == original
            assert (
                db.execute(
                    text(
                        "SELECT bool_and(provider_drama_id=drama_id) FROM preview_drama"
                    )
                ).scalar_one()
                is True
            )
            assert (
                db.execute(
                    text("SELECT count(*) FROM campaign_copy_source_facts")
                ).scalar_one()
                == 0
            )
            assert (
                db.execute(
                    text(
                        "SELECT tgenabled FROM pg_trigger WHERE tgname='preview_drama_frozen'"
                    )
                ).scalar_one()
                == "O"
            )
        with engine.begin() as db, pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE preview_drama SET title='cannot mutate frozen intent'")
            )
        with engine.begin() as db, pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE build_draft SET strategy_version_id=NULL,provider_connection_id=NULL,application_id=NULL"
                )
            )
