from alembic import command
from sqlalchemy import inspect

from tests.migration_database import historical_database


def test_skipped_material_migration_preserves_empty_historical_selection(monkeypatch):
    with historical_database(monkeypatch, "material_seed_generations") as (
        engine,
        config,
    ):
        command.upgrade(config, "preview_skipped_materials")
        assert "preview_skipped_material" in inspect(engine).get_table_names()
        command.downgrade(config, "material_seed_generations")
        assert "preview_skipped_material" not in inspect(engine).get_table_names()
        # 先验证本迁移往返；最新报表迁移不可逆，只做向前升级与元数据检查。
        command.upgrade(config, "head")
        command.check(config)
