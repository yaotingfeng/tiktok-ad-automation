# backend/app/alembic/versions/0023_reporting_store.py
"""New per-account reporting store: partitioned facts, sync state, jobs and plans."""

from alembic import op

revision = "0023_reporting_store"
down_revision = "0022_provider_link_defaults"
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
    op.execute(f"""
    CREATE TABLE ad_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        ad_id varchar(64) NOT NULL, campaign_id varchar(64) NOT NULL, adgroup_id varchar(64) NOT NULL,
        campaign_automation_type varchar(64), ad_name varchar, campaign_name varchar, adgroup_name varchar,
        {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, ad_id)
    ) PARTITION BY RANGE (stat_date);
    CREATE TABLE ad_report_day_default PARTITION OF ad_report_day DEFAULT;
    CREATE INDEX ix_ad_report_day_period ON ad_report_day (tenant_id, stat_date, advertiser_id);
    CREATE INDEX ix_ad_report_day_campaign ON ad_report_day (tenant_id, campaign_id, stat_date);

    CREATE TABLE material_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        main_material_id varchar(64) NOT NULL, main_material_type varchar(64) NOT NULL,
        main_material_name varchar,
        {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, main_material_id)
    ) PARTITION BY RANGE (stat_date);
    CREATE TABLE material_report_day_default PARTITION OF material_report_day DEFAULT;
    CREATE INDEX ix_material_report_day_period ON material_report_day (tenant_id, stat_date, advertiser_id);
    CREATE INDEX ix_material_report_day_material ON material_report_day (tenant_id, main_material_id, stat_date);

    CREATE TABLE campaign_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        campaign_id varchar(64) NOT NULL, campaign_name varchar, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, campaign_id)
    );
    CREATE INDEX ix_campaign_report_day_period ON campaign_report_day (tenant_id, stat_date, advertiser_id);

    CREATE TABLE campaign_report_day_utc (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        campaign_id varchar(64) NOT NULL, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, campaign_id)
    );

    CREATE TABLE campaign_report_hour (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_hour timestamptz NOT NULL,
        campaign_id varchar(64) NOT NULL, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_hour, campaign_id)
    );

    CREATE TABLE account_reconciliation_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        account_spend numeric, ad_sum_spend numeric, account_revenue_total numeric,
        ad_sum_revenue_total numeric, checked_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, advertiser_id, stat_date)
    );

    CREATE TABLE report_sync_state (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, report_kind varchar(32) NOT NULL,
        stat_date date NOT NULL, status varchar(16) NOT NULL, row_count integer NOT NULL DEFAULT 0,
        last_success_at timestamptz, error_code varchar(128), updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, advertiser_id, report_kind, stat_date),
        CONSTRAINT ck_report_sync_state_status CHECK (status IN ('COMPLETE','STALE','FAILED'))
    );

    CREATE TABLE report_sync_job (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, bc_id varchar(128) NOT NULL,
        kind varchar(32) NOT NULL, priority integer NOT NULL, status varchar(16) NOT NULL,
        advertiser_ids jsonb NOT NULL, campaign_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        start_date date NOT NULL, end_date date NOT NULL, job_key varchar(128) NOT NULL,
        frozen_route jsonb NOT NULL, actor_id uuid NOT NULL, due_at timestamptz NOT NULL,
        claim_token uuid, claimed_until timestamptz, attempts integer NOT NULL DEFAULT 0,
        error_code varchar(128), request_count integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL, finished_at timestamptz,
        CONSTRAINT ck_report_sync_job_status CHECK
            (status IN ('QUEUED','RUNNING','WAITING','COMPLETE','FAILED','CANCELLED'))
    );
    CREATE INDEX ix_report_sync_job_due ON report_sync_job (status, priority, due_at);
    CREATE UNIQUE INDEX uq_report_sync_job_active_key ON report_sync_job (job_key)
        WHERE status IN ('QUEUED','RUNNING','WAITING');

    CREATE TABLE ad_sync_bc (
        tenant_id uuid NOT NULL, bc_id varchar(128) NOT NULL, frozen_route jsonb NOT NULL,
        actor_id uuid NOT NULL, enabled boolean NOT NULL DEFAULT true, disabled_reason varchar(128),
        bc_report_supported boolean, next_hot_at timestamptz NOT NULL,
        next_material_at timestamptz NOT NULL, next_directory_at timestamptz NOT NULL,
        next_daily_at timestamptz NOT NULL, last_active_advertiser_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, bc_id),
        FOREIGN KEY (tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id)
    );

    CREATE TABLE ad_sync_account (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, owner_bc_id varchar(128) NOT NULL,
        history_planned_at timestamptz,
        PRIMARY KEY (tenant_id, advertiser_id)
    );
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE ad_sync_account; DROP TABLE ad_sync_bc; DROP TABLE report_sync_job;
    DROP TABLE report_sync_state; DROP TABLE account_reconciliation_day;
    DROP TABLE campaign_report_hour; DROP TABLE campaign_report_day_utc;
    DROP TABLE campaign_report_day; DROP TABLE material_report_day; DROP TABLE ad_report_day;
    """)
