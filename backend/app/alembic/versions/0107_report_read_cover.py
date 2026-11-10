"""Cover report metrics and frozen material membership without wide heap reads."""

from alembic import op

revision = "0107_report_read_cover"
down_revision = "0104_operation_logs"
branch_labels = None
depends_on = None

METRICS = (
    "spend",
    "impressions",
    "clicks",
    "ad_impression_events",
    "revenue_d0",
    "revenue_d3",
    "revenue_d7",
    "revenue_d14",
    "revenue_d30",
    "revenue_total",
)


def upgrade() -> None:
    included = ",".join(
        ("remote_id", "variant", "currency", "timezone")
        + METRICS
        + tuple(f"{metric}_has_null" for metric in METRICS)
    )
    op.execute(f"""
        CREATE INDEX ix_entity_metric_day_period_cover
        ON entity_metric_day(tenant_id,kind,stat_date,advertiser_id)
        INCLUDE({included})
    """)
    op.execute("DROP INDEX ix_entity_metric_day_period")
    op.execute("""
        ALTER INDEX ix_entity_metric_day_period_cover
        RENAME TO ix_entity_metric_day_period
    """)
    op.execute("""
        CREATE INDEX ix_material_account_member_video_cover
        ON material_account_member_current(tenant_id,platform_material_id,advertiser_id)
        INCLUDE(version_id,use_count)
    """)
    op.execute("DROP INDEX ix_material_account_member_video")
    op.execute("""
        ALTER INDEX ix_material_account_member_video_cover
        RENAME TO ix_material_account_member_video
    """)


def downgrade() -> None:
    op.execute("""
        CREATE INDEX ix_entity_metric_day_period_keys
        ON entity_metric_day(tenant_id,kind,stat_date,advertiser_id)
    """)
    op.execute("DROP INDEX ix_entity_metric_day_period")
    op.execute("""
        ALTER INDEX ix_entity_metric_day_period_keys
        RENAME TO ix_entity_metric_day_period
    """)
    op.execute("""
        CREATE INDEX ix_material_account_member_video_keys
        ON material_account_member_current(tenant_id,platform_material_id,advertiser_id)
    """)
    op.execute("DROP INDEX ix_material_account_member_video")
    op.execute("""
        ALTER INDEX ix_material_account_member_video_keys
        RENAME TO ix_material_account_member_video
    """)
