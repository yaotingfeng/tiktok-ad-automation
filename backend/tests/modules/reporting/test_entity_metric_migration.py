"""迁移从既有源集合恢复金额，逆迁移只删除可重建的派生对象。"""

import importlib
from datetime import date

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.modules.reporting.test_entity_metrics import SOURCES, compact, insert_source


def test_entity_metric_migration_round_trip_retains_sources_and_sparse_months(
    session, sync_seed, monkeypatch
):
    migration = importlib.import_module("app.alembic.versions.0105_entity_metric_days")
    monkeypatch.setattr(
        migration, "op", Operations(MigrationContext.configure(session.connection()))
    )
    tenant = sync_seed.tenant_id
    for kind in SOURCES:
        insert_source(session, tenant, kind, day=date(1999, 3, 4))
    insert_source(session, tenant, "campaign", day=date(2055, 11, 3), remote="future")
    insert_source(session, tenant, "ad", day=date(1888, 1, 2), advertiser="orphan")
    source_before = {
        kind: session.execute(
            text(
                f"SELECT to_jsonb(f) FROM {table} f WHERE tenant_id=:t ORDER BY stat_date,{identity}"
            ),
            {"t": tenant},
        ).all()
        for kind, (table, identity) in SOURCES.items()
    }
    migration.downgrade()
    assert (
        session.execute(text("SELECT to_regclass('entity_metric_day')")).scalar_one()
        is None
    )
    migration.upgrade()
    for kind, (table, identity) in SOURCES.items():
        assert (
            session.execute(
                text(
                    f"SELECT to_jsonb(f) FROM {table} f WHERE tenant_id=:t ORDER BY stat_date,{identity}"
                ),
                {"t": tenant},
            ).all()
            == source_before[kind]
        )
        rows = compact(session, tenant, kind)
        assert len(rows) == (2 if kind == "campaign" else 1)
        assert all(
            row["spend_has_null"] is False and row["revenue_d3_has_null"]
            for row in rows
        )
    partitions = set(
        session.execute(
            text("""SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
        WHERE i.inhparent='entity_metric_day'::regclass""")
        ).scalars()
    )
    assert "entity_metric_day_1999_03" in partitions
    assert "entity_metric_day_2055_11" in partitions
    assert "entity_metric_day_default" in partitions
    assert (
        "entity_metric_day_1888_01" not in partitions
    )  # 孤立历史不会占用分区和派生行。
    assert "entity_metric_day_2000_01" not in partitions
    assert len(partitions) <= 7  # 两个历史月份、当前及未来三个月、DEFAULT。
    insert_source(session, tenant, "campaign", remote="after-round-trip")
    assert len(compact(session, tenant, "campaign")) == 3


def test_entity_metric_model_and_sql_views_agree(session):
    from app.modules.reporting.read_models.entity_metrics import EntityMetricDay

    assert EntityMetricDay.__table__.c.remote_id.type.length == 512
    assert not set(EntityMetricDay.__table__.c.keys()) & {
        "campaign_name",
        "ad_name",
        "post_names",
    }
    for kind, (_, identity) in SOURCES.items():
        names = set(
            session.execute(
                text("""SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:name"""),
                {"name": f"{kind}_metric_day"},
            ).scalars()
        )
        assert {
            identity,
            "spend",
            "spend_has_null",
            "source_revision",
            "fetched_at",
        } <= names
        assert not names & {
            "campaign_name",
            "adgroup_name",
            "ad_name",
            "post_names",
            "post_ids",
        }
        if kind == "material":
            assert "main_material_type" in names
