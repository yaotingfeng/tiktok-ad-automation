"""Drop the unused UTC campaign-day report store and its sync bookkeeping."""

from alembic import op

revision = "0025_drop_campaign_day_utc"
down_revision = "0024_drop_legacy_reporting"
branch_labels = None
depends_on = None

_METRICS = """
    spend numeric, impressions numeric, clicks numeric, ad_impression_events numeric,
    revenue_d0 numeric, revenue_d3 numeric, revenue_d7 numeric, revenue_d14 numeric,
    revenue_d30 numeric, revenue_total numeric,
    currency varchar(16) NOT NULL, timezone varchar(64) NOT NULL,
    fetched_at timestamptz NOT NULL, sync_job_id uuid NOT NULL
"""


def upgrade() -> None:
    # UTC 系列日没有任何读取方；未结束的任务取消，否则新版本 Worker 无法执行该类型。
    op.execute("""
    UPDATE report_sync_job
       SET status = 'CANCELLED', claim_token = NULL, claimed_until = NULL,
           finished_at = now(), error_code = 'report_kind_removed'
     WHERE kind = 'campaign_day_utc' AND status IN ('QUEUED','RUNNING','WAITING');
    DELETE FROM report_sync_state WHERE report_kind = 'campaign_day_utc';
    DROP TABLE campaign_report_day_utc;
    """)


def downgrade() -> None:
    # 只恢复表结构；数据由旧版本回刷重新拉取，取消的任务不恢复。
    op.execute(f"""
    CREATE TABLE campaign_report_day_utc (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        campaign_id varchar(64) NOT NULL, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, campaign_id)
    );
    """)
