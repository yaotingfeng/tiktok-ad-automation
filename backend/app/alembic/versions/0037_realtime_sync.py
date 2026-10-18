"""实时同步关联游标与持久健康告警。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_realtime_sync"
down_revision = "0036_drop_rule_safety_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "report_sync_job",
        sa.Column("progress", postgresql.JSONB(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "report_sync_health",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("bc_id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_repair_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
    )
    op.create_table(
        "report_sync_health_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_report_sync_health_event_scope",
        "report_sync_health_event",
        ["tenant_id", "bc_id", "created_at"],
    )

    op.create_table(
        "report_sync_page",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("page", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.Column("row_keys", postgresql.JSONB(), nullable=False),
        sa.Column("total_page", sa.Integer(), nullable=False),
        sa.Column("total_number", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["report_sync_job.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "page >= 1 AND total_page >= 0 AND total_number >= 0",
            name="ck_report_sync_page_numbers",
        ),
    )
    op.create_index(
        "ix_report_sync_page_row_keys",
        "report_sync_page",
        ["row_keys"],
        postgresql_using="gin",
    )

    op.add_column(
        "ad_sync_bc",
        sa.Column("active_screened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "directory_dispatch_cursor",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("directory_dispatch_cursor")
    op.drop_column("ad_sync_bc", "active_screened_at")
    op.drop_table("report_sync_page")
    op.drop_table("report_sync_health_event")
    op.drop_table("report_sync_health")
    op.drop_column("report_sync_job", "progress")
