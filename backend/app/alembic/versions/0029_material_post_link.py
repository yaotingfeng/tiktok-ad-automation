"""Map material-report main material IDs (Spark post IDs) to their ads."""

from alembic import op

revision = "0029_material_post_link"
down_revision = "0028_automation_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 只新增对照表，不改素材日报；历史素材数据由同步任务按账户补齐对照。
    op.execute("""
    CREATE TABLE material_post_link (
        tenant_id uuid NOT NULL,
        advertiser_id varchar(128) NOT NULL,
        main_material_id varchar(64) NOT NULL,
        ad_remote_id varchar(128) NOT NULL,
        main_material_type varchar(64) NOT NULL,
        ad_material_id varchar(128),
        material_name varchar,
        platform_material_id varchar(128),
        observed_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, advertiser_id, main_material_id, ad_remote_id)
    );
    CREATE INDEX ix_material_post_link_ad
        ON material_post_link (tenant_id, advertiser_id, ad_remote_id);
    CREATE TABLE material_link_scan (
        tenant_id uuid NOT NULL,
        advertiser_id varchar(128) NOT NULL,
        scanned_at timestamptz NOT NULL,
        unresolved_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        PRIMARY KEY (tenant_id, advertiser_id)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE material_link_scan; DROP TABLE material_post_link;")
