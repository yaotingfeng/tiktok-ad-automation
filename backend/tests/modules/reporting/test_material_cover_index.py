import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.modules.reporting.test_material_query import (
    material_row,
    publish,
    query,
    use,
)


def test_cover_index_roundtrip_preserves_members_and_supports_index_only_reads(
    session, sync_seed
):
    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    use(
        session,
        sync_seed,
        adv="acc-1",
        ad="ad",
        video="video",
        ad_material="position",
        post="post",
    )
    before = query(session, sync_seed, include_empty=False)
    path = (
        Path(__file__).resolve().parents[3]
        / "app/alembic/versions/0107_report_read_cover.py"
    )
    spec = importlib.util.spec_from_file_location("material_cover_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert query(session, sync_seed, include_empty=False) == before
        migration.upgrade()
    assert query(session, sync_seed, include_empty=False) == before
    connection.execute(text("SET LOCAL enable_seqscan=off"))
    plan = connection.execute(
        text("""
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT advertiser_id,platform_material_id,version_id,use_count
            FROM material_account_member_current
            WHERE tenant_id=:tenant AND platform_material_id=:video
        """),
        {"tenant": sync_seed.tenant_id, "video": "video"},
    ).scalar_one()[0]["Plan"]
    assert plan["Node Type"] == "Index Only Scan", plan
    assert plan["Actual Rows"] == 1
    connection.execute(text("SET LOCAL enable_bitmapscan=off"))
    metric_plan = connection.execute(
        text("""
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT remote_id,variant,currency,timezone,spend,spend_has_null
            FROM entity_metric_day
            WHERE tenant_id=:tenant AND kind='material'
                AND stat_date='2026-01-01'
        """),
        {"tenant": sync_seed.tenant_id},
    ).scalar_one()[0]["Plan"]
    assert metric_plan["Node Type"] == "Index Only Scan", metric_plan
    connection.execute(
        text("""
        CREATE TABLE report_cover_future_partition PARTITION OF entity_metric_day
        FOR VALUES FROM ('2040-01-01') TO ('2040-02-01')
    """)
    )
    inherited = connection.execute(
        text("""
        SELECT pg_get_indexdef(child.oid)
        FROM pg_inherits inheritance
        JOIN pg_class child ON child.oid=inheritance.inhrelid
        JOIN pg_class parent ON parent.oid=inheritance.inhparent
        JOIN pg_index entry ON entry.indexrelid=child.oid
        WHERE parent.relname='ix_entity_metric_day_period'
            AND entry.indrelid='report_cover_future_partition'::regclass
    """)
    ).scalar_one()
    assert "INCLUDE (remote_id, variant, currency, timezone, spend," in inherited
