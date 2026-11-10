"""Independent partitioned creative facts; legacy material facts remain intact."""
from alembic import op

revision = "0112_creative_report_day"
down_revision = "0111_ad_creative"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE creative_report_day (
            tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL,
            stat_date date NOT NULL, creative_id varchar(128) NOT NULL,
            campaign_id varchar(128) NOT NULL, adgroup_id varchar(128) NOT NULL,
            campaign_automation_type varchar(64),
            spend numeric, impressions numeric, clicks numeric, ad_impression_events numeric,
            revenue_d0 numeric, revenue_d3 numeric, revenue_d7 numeric, revenue_d14 numeric,
            revenue_d30 numeric, revenue_total numeric,
            currency varchar(16) NOT NULL, timezone varchar(64) NOT NULL,
            fetched_at timestamptz NOT NULL, sync_job_id uuid NOT NULL,
            PRIMARY KEY (tenant_id,advertiser_id,stat_date,creative_id)
        ) PARTITION BY RANGE(stat_date)
    """)
    op.execute("CREATE TABLE creative_report_day_default PARTITION OF creative_report_day DEFAULT")
    op.execute("CREATE INDEX ix_creative_report_day_period ON creative_report_day (tenant_id,stat_date,advertiser_id)")
    op.execute("CREATE INDEX ix_creative_report_day_group ON creative_report_day (tenant_id,advertiser_id,adgroup_id,stat_date)")
    op.execute("CREATE INDEX ix_creative_report_default_date ON creative_report_day_default (stat_date)")


def downgrade() -> None:
    op.drop_table("creative_report_day")
