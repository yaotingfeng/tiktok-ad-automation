"""Persist AI optimizer scheduled build handoffs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0121_ai_build_occurrences"
down_revision = "0120_ai_workbench_daily_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_build_occurrence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_row_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("advertiser_ids", JSONB(), nullable=False),
        sa.Column("provider_connection_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("promotion_url", sa.String(length=8192), nullable=True),
        sa.Column("external_drama_id", sa.String(length=255), nullable=True),
        sa.Column("order_materials", sa.Boolean(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("preview_id", sa.Uuid(), nullable=True),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_build_occurrence_optimizer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_row_id"],
            ["ai_daily_plan_row.tenant_id", "ai_daily_plan_row.id"],
            name="fk_ai_build_occurrence_plan_row",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
            name="fk_ai_build_occurrence_strategy",
        ),
        sa.ForeignKeyConstraint(["dispatch_id"], ["pending_dispatch.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_build_occurrence_tenant_id"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_ai_build_occurrence_request"),
        sa.UniqueConstraint(
            "tenant_id", "optimizer_id", "plan_row_id", "strategy_version_id", "scheduled_at",
            name="uq_ai_build_occurrence_schedule",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','WAITING_MATERIAL','DISPATCHED','PARTIAL','UNKNOWN','COMPLETE','FAILED')",
            name="ck_ai_build_occurrence_status",
        ),
    )
    op.create_index(
        "ix_ai_build_occurrence_due", "ai_build_occurrence", ["status", "scheduled_at", "id"]
    )
    op.create_index(
        "ix_ai_build_occurrence_optimizer", "ai_build_occurrence", ["tenant_id", "optimizer_id", "scheduled_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_build_occurrence_optimizer", table_name="ai_build_occurrence")
    op.drop_index("ix_ai_build_occurrence_due", table_name="ai_build_occurrence")
    op.drop_table("ai_build_occurrence")
