"""Immutable rate schedules; preserve legacy effective-date accounting exactly."""

from alembic import op

revision = "0102_business_rate_periods"
down_revision = "0101_business_dashboard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 父分区表的约束变更同步传播到所有日分区，明确区分未知返点与0%。
    op.execute("ALTER TABLE business_day ALTER COLUMN rebate_rate DROP NOT NULL")
    op.execute('''
        CREATE TABLE business_rate_schedule (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenant(id),
            kind VARCHAR(16) NOT NULL,
            scope_id VARCHAR(128) NOT NULL,
            provider_id UUID,
            bc_id VARCHAR(128),
            revision INTEGER NOT NULL,
            periods JSONB NOT NULL,
            operation JSONB NOT NULL,
            actor_id UUID NOT NULL REFERENCES "user"(id),
            created_at TIMESTAMPTZ NOT NULL,
            FOREIGN KEY(tenant_id, provider_id) REFERENCES provider_connection(tenant_id, id),
            FOREIGN KEY(tenant_id, bc_id) REFERENCES tenant_bc(tenant_id, bc_id),
            CONSTRAINT ck_rate_schedule_scope CHECK (
                (kind='provider' AND provider_id IS NOT NULL AND bc_id IS NULL) OR
                (kind='rebate' AND bc_id IS NOT NULL AND provider_id IS NULL)),
            CONSTRAINT uq_rate_schedule_revision UNIQUE(tenant_id, kind, scope_id, revision)
        )
    ''')
    # 同日最高修订胜出，不按最后保存日期覆盖更晚生效日期；原记录完整保留。
    op.execute('''
        WITH winners AS (
            SELECT DISTINCT ON (tenant_id, kind, scope_id, effective_date) *
            FROM business_rate ORDER BY tenant_id, kind, scope_id, effective_date, revision DESC
        ), spans AS (
            SELECT *, lead(effective_date) OVER (
                PARTITION BY tenant_id, kind, scope_id ORDER BY effective_date
            ) - 1 AS end_date FROM winners
        ), timelines AS (
            SELECT tenant_id, kind, scope_id, jsonb_agg(jsonb_build_object(
                'start_date', effective_date::text, 'end_date', end_date::text, 'rate', rate::text
            ) ORDER BY effective_date) AS periods
            FROM spans GROUP BY tenant_id, kind, scope_id
        ), latest AS (
            SELECT DISTINCT ON (tenant_id, kind, scope_id) *
            FROM business_rate ORDER BY tenant_id, kind, scope_id, revision DESC
        )
        INSERT INTO business_rate_schedule
        SELECT gen_random_uuid(), l.tenant_id, l.kind, l.scope_id, l.provider_id, l.bc_id,
            l.revision, t.periods, '{"note":"旧配置迁移","action":"migration"}'::jsonb,
            l.actor_id, l.created_at
        FROM latest l JOIN timelines t USING(tenant_id, kind, scope_id)
    ''')
    op.execute("UPDATE business_scope SET revision=revision+1, last_daily=NULL, next_check_at=now()")
    op.execute("UPDATE business_run SET status='SUPERSEDED' WHERE status IN ('QUEUED','RUNNING')")


def downgrade() -> None:
    # 新日程包含终止和空档，旧模型无法无损表达；存在新操作时拒绝破坏性降级。
    op.execute('''DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM business_rate_schedule WHERE operation->>'action' <> 'migration') THEN
            RAISE EXCEPTION 'rate schedules have edits; forward migration required';
        END IF;
    END $$''')
    op.execute("DROP TABLE business_rate_schedule")
    op.execute("ALTER TABLE business_day ALTER COLUMN rebate_rate SET NOT NULL")
