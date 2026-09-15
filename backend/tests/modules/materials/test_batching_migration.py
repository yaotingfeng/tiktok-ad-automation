"""从上一发布 head 迁移，确认批次与补建事实拥有数据库级保护。"""

from alembic import command
from sqlalchemy import inspect, text

from tests.migration_database import historical_database


def test_previous_release_upgrades_batching_schema_without_rewriting_history(
    monkeypatch,
):
    with historical_database(monkeypatch, "provider_display_drama_id") as (
        engine,
        config,
    ):
        assert "material_share_batch" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names())
        assert {
            "material_share_batch",
            "material_share_batch_member",
            "material_share_batch_receipt",
            "build_verified_replacement",
        } <= tables
        with engine.connect() as db:
            guards = set(
                db.execute(
                    text("""
                SELECT tgname FROM pg_trigger WHERE NOT tgisinternal
            """)
                ).scalars()
            )
            assert {
                "material_share_batch_frozen",
                "material_share_member_frozen",
                "material_share_receipt_immutable",
                "build_replacement_immutable",
            } <= guards
