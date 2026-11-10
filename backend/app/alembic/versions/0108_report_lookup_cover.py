"""Cover complete report buckets and usable post membership lookups."""

from alembic import op

revision = "0108_report_lookup_cover"
down_revision = "0107_report_read_cover"
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
        ("currency", "timezone", "fetched_at", "campaign_name") + METRICS
    )
    op.execute(f"""
        CREATE INDEX ix_campaign_report_day_object
        ON campaign_report_day(tenant_id,advertiser_id,campaign_id,stat_date)
        INCLUDE({included})
    """)
    op.execute("""
        CREATE INDEX ix_read_bucket_complete_period
        ON read_bucket_state(tenant_id,kind,bucket,advertiser_id)
        WHERE derived_revision>0 AND derived_status='COMPLETE'
    """)
    op.execute("""
        CREATE INDEX ix_material_post_link_member
        ON material_post_link(tenant_id,advertiser_id,main_material_id,main_material_type)
        INCLUDE(ad_remote_id,ad_material_id) WHERE ad_material_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_material_post_link_member")
    op.execute("DROP INDEX ix_read_bucket_complete_period")
    op.execute("DROP INDEX ix_campaign_report_day_object")
