"""选择偏好迁移保留旧草稿，禁止跨范围关联和静默丢失已选连接。"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows


def test_draft_connection_migration_preserves_history_and_guards_scope_and_downgrade(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_build_recovery") as (engine, config):
        with Session(engine) as session, session.begin():
            context, preview, _ = historical_rows(session, connections=0)
            from tests.modules.builds.test_drafts import account

            account(session, context)
            other, other_preview, _ = historical_rows(session, connections=0)
            account(session, other)
            draft_id = preview.draft_id
        query = text(
            "SELECT to_jsonb(d) - 'execution_connection_id' FROM build_draft d ORDER BY id"
        )
        with engine.connect() as db:
            original = db.execute(query).all()
            chosen = db.execute(
                text(
                    "SELECT connection_id FROM bc_default_route WHERE tenant_id=:tenant"
                ),
                {"tenant": context.tenant_id},
            ).scalar_one()
            wrong = db.execute(
                text(
                    "SELECT connection_id FROM bc_default_route WHERE tenant_id=:tenant"
                ),
                {"tenant": other.tenant_id},
            ).scalar_one()
        command.upgrade(config, "mcp_draft_connection")
        with engine.connect() as db:
            assert db.execute(query).all() == original
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM build_draft WHERE execution_connection_id IS NOT NULL"
                    )
                ).scalar_one()
                == 0
            )
        with engine.begin() as db, pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE build_draft SET execution_connection_id=:choice WHERE id=:draft"
                ),
                {"choice": wrong, "draft": draft_id},
            )
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE build_draft SET execution_connection_id=:choice WHERE id=:draft"
                ),
                {"choice": chosen, "draft": draft_id},
            )
        with pytest.raises(
            DBAPIError, match="Cannot drop explicit draft connection choices"
        ):
            command.downgrade(config, "mcp_build_recovery")
        with engine.begin() as db:
            assert db.execute(query).all() == original
            db.execute(
                text(
                    "UPDATE build_draft SET execution_connection_id=NULL WHERE id=:draft"
                ),
                {"draft": draft_id},
            )
        command.downgrade(config, "mcp_build_recovery")
        with engine.connect() as db:
            assert db.execute(query).all() == original
