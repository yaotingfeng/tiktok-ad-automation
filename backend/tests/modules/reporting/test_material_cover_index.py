import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.modules.reporting.test_material_query import (
    D,
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


def test_lookup_cover_roundtrip_preserves_coverage_and_post_members(session, sync_seed):
    from tests.modules.reporting.test_dimension_query import _seed

    _seed(session, sync_seed)
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
        / "app/alembic/versions/0108_report_lookup_cover.py"
    )
    spec = importlib.util.spec_from_file_location("lookup_cover_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert query(session, sync_seed, include_empty=False) == before
        migration.upgrade()
    assert query(session, sync_seed, include_empty=False) == before
    connection.execute(text("SET LOCAL enable_seqscan=off"))
    connection.execute(text("SET LOCAL enable_bitmapscan=off"))
    for statement in (
        """SELECT campaign_name,currency,timezone,fetched_at,spend
            FROM campaign_report_day WHERE tenant_id=:tenant
                AND advertiser_id='acc-1' AND campaign_id='c1'
                AND stat_date='2026-10-05'""",
        """SELECT advertiser_id,bucket FROM read_bucket_state
            WHERE tenant_id=:tenant AND kind='material_day'
                AND bucket='2026-10-05' AND derived_revision>0
                AND derived_status='COMPLETE'""",
        """SELECT advertiser_id,ad_remote_id,ad_material_id FROM material_post_link
            WHERE tenant_id=:tenant AND advertiser_id='acc-1'
                AND main_material_id='post' AND main_material_type='VIDEO_SPARK_ADS'
                AND ad_material_id IS NOT NULL""",
    ):
        plan = connection.execute(
            text("EXPLAIN (ANALYZE,FORMAT JSON) " + statement),
            {"tenant": sync_seed.tenant_id},
        ).scalar_one()[0]["Plan"]
        assert plan["Node Type"] == "Index Only Scan", plan
        assert plan["Actual Rows"] == 1


def test_material_coverage_query_uses_covering_index_and_keeps_missing_accounts(
    session, sync_seed
):
    from app.modules.reporting.row_set import _common_ctes
    from tests.modules.reporting.query_helpers import finish_projections

    publish(session, sync_seed, "acc-1", [material_row("post", "10")])
    finish_projections(session)
    statement = "WITH " + _common_ctes("material_day") + " SELECT * FROM cov"
    parameters = {
        "tenant": sync_seed.tenant_id,
        "advs": ["acc-1", "acc-2"],
        "start": D,
        "end": D,
        "ndays": 1,
    }
    assert dict(session.execute(text(statement), parameters).all()) == {
        "acc-1": True,
        "acc-2": False,
    }
    # 构造真实的跨期历史，避免单行表中两个等价索引的成本并列使计划断言随机。
    session.execute(
        text("""INSERT INTO read_bucket_state
        (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at,derived_observed_at,derived_status)
        SELECT :tenant,'acc-1','material_day',CAST(:start AS date)-n,1,1,now(),now(),'COMPLETE'
        FROM generate_series(1,1000) n ON CONFLICT DO NOTHING"""),
        parameters,
    )
    session.execute(text("ANALYZE read_bucket_state"))
    session.execute(text("SET LOCAL enable_seqscan=off"))
    session.execute(text("SET LOCAL enable_bitmapscan=off"))
    plan = session.execute(
        text("EXPLAIN (ANALYZE,FORMAT JSON) " + statement), parameters
    ).scalar_one()[0]["Plan"]

    def scans(node):
        return (
            [node]
            if node.get("Relation Name") == "read_bucket_state"
            else [scan for child in node.get("Plans", []) for scan in scans(child)]
        )

    state_scans = scans(plan)
    assert state_scans
    assert all(scan["Node Type"] == "Index Only Scan" for scan in state_scans), (
        state_scans
    )
    assert all(
        scan["Index Name"] == "ix_read_bucket_projection_period" for scan in state_scans
    )
