"""遇到首条未承接关系即停止，不能先整理整个父广告的历史素材。"""

from importlib import import_module

from sqlalchemy import text
from sqlmodel import Session

from tests.migration_database import historical_database
from tests.modules.reporting.sync_conftest import sync_seed


def test_missing_identity_stops_before_reading_parent_asset_history(
    monkeypatch, record_property
):
    migration = import_module("app.alembic.versions.0116_drop_legacy_material")
    with historical_database(monkeypatch, "0115_copy_current_source") as (
        engine,
        _config,
    ):
        with Session(engine) as session, session.begin():
            seed = sync_seed.__wrapped__(session)
            # 相同租户、账户、父广告和广告内素材ID，1000个不同资产关系。
            # 每一条都缺失同一官方CID，拒绝删除不应先读完这一父组。
            session.execute(
                text("""
                INSERT INTO ad_material_reference
                  (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,
                   ad_material_id,material_type,name,creative_ids,complete,published_version)
                SELECT gen_random_uuid(),:tenant,'acc-1','one-parent',
                  'asset-'||i,'same-ad-material','IMAGE','legacy',
                  '["missing-cid"]'::jsonb,true,1
                FROM generate_series(1,1000) i
                """),
                {"tenant": seed.tenant_id},
            )
            session.execute(text("ANALYZE ad_material_reference"))
            session.execute(text("ANALYZE ad_creative"))
            actual = session.execute(text(migration.IDENTITY_COVERAGE_SQL)).all()
            assert actual == [(seed.tenant_id, "acc-1", "missing-cid")]
            plan = session.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    + migration.IDENTITY_COVERAGE_SQL
                )
            ).scalar_one()[0]

        def scanned(node):
            own = 0
            if node.get("Relation Name") == "ad_material_reference":
                own = (
                    node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                ) * node.get("Actual Loops", 1)
            return own + sum(scanned(child) for child in node.get("Plans", []))

        source_rows = scanned(plan["Plan"])
        record_property("legacy_parent_asset_rows", 1000)
        record_property("guard_source_rows_processed", source_rows)
        record_property("guard_execution_ms", plan["Execution Time"])
        assert source_rows <= 10, plan
