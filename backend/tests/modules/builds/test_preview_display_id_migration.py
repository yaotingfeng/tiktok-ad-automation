"""广告命名展示编号迁移不猜测或改写历史预览。"""

from alembic import command
from sqlalchemy import inspect

from tests.migration_database import historical_database


def test_preview_display_id_migration_adds_empty_frozen_field(monkeypatch):
    with historical_database(monkeypatch, "build_batching") as (engine, config):
        command.upgrade(config, "preview_display_drama_id")
        column = next(
            value
            for value in inspect(engine).get_columns("preview_drama")
            if value["name"] == "display_drama_id"
        )
        assert column["nullable"] is False
        assert column["default"] == "''::character varying"

        command.downgrade(config, "build_batching")
        assert "display_drama_id" not in {
            value["name"] for value in inspect(engine).get_columns("preview_drama")
        }
