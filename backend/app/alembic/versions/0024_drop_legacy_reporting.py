"""Drop legacy reporting facts, coverage, staging, observations, runs and schedules."""

from alembic import op

revision = "0024_drop_legacy_reporting"
down_revision = "0023_reporting_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 大 BC 回退筛选需要持久游标；保留已完成请求，避免反复耗尽同一轮期限。
    op.execute(
        "ALTER TABLE ad_sync_bc ADD COLUMN screening_progress JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    # 余额按 BC 续页，冻结账户集合不能塞进有界 outbox 载荷。
    op.execute("""
    CREATE TABLE report_balance_run (
        id UUID PRIMARY KEY, tenant_id UUID NOT NULL, actor_id UUID NOT NULL,
        run_key VARCHAR(128) NOT NULL, frozen_route JSONB NOT NULL,
        remaining_advertiser_ids JSONB NOT NULL, next_page INTEGER NOT NULL,
        status VARCHAR(16) NOT NULL, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE UNIQUE INDEX uq_report_balance_run_key ON report_balance_run (tenant_id, run_key);
    """)
    # 旧事实不迁移：新存储由历史回补重建；仅实际发布时由人工执行迁移。
    op.execute("""
    DROP TABLE IF EXISTS report_observation, report_fact, report_coverage,
        report_staged_page, report_sync_run, sync_schedule CASCADE;
    """)


def downgrade() -> None:
    raise RuntimeError(
        "legacy reporting tables are not restored; restore from the pre-release backup"
    )
