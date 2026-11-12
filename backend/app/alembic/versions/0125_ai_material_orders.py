"""Persistent material-order idempotency and delivery receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0125_ai_material_orders"
down_revision = "0124_ai_runtime_completion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_ai_daily_plan_row_dispatch",
        "ai_daily_plan_row",
        ["business_date", "scheduled_time", "id"],
    )
    op.add_column(
        "ai_optimizer_version",
        sa.Column("material_config", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "ai_material_order",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_row_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("request_body", JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("job_id", sa.String(255)),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "plan_row_id", name="uq_ai_material_order_row"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_row_id"],
            ["ai_daily_plan_row.tenant_id", "ai_daily_plan_row.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"], ["ai_optimizer.tenant_id", "ai_optimizer.id"]
        ),
    )
    op.create_index(
        "ix_ai_material_order_due", "ai_material_order", ["status", "next_attempt_at"]
    )


def downgrade():
    op.drop_index("ix_ai_daily_plan_row_dispatch", table_name="ai_daily_plan_row")
    op.drop_table("ai_material_order")
    op.drop_column("ai_optimizer_version", "material_config")
