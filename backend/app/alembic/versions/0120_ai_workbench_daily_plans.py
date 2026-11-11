"""Persist tenant-scoped AI optimizer daily drama plans."""

import sqlalchemy as sa
from alembic import op

revision = "0120_ai_workbench_daily_plans"
down_revision = "0119_ai_workbench_optimizer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_daily_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_daily_plan_optimizer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_daily_plan_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "optimizer_id", "business_date", name="uq_ai_daily_plan_day"
        ),
        sa.CheckConstraint("revision > 0", name="ck_ai_daily_plan_revision"),
    )
    op.create_index(
        "ix_ai_daily_plan_optimizer_day",
        "ai_daily_plan",
        ["tenant_id", "optimizer_id", "business_date"],
    )
    op.create_table(
        "ai_daily_plan_row",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("scheduled_time", sa.Time(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("provider_connection_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.String(length=255), nullable=False),
        sa.Column("promotion_url", sa.String(length=8192), nullable=True),
        sa.Column("external_drama_id", sa.String(length=255), nullable=True),
        sa.Column("order_materials", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["ai_daily_plan.tenant_id", "ai_daily_plan.id"],
            name="fk_ai_daily_plan_row_plan",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_daily_plan_row_optimizer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_ai_daily_plan_row_provider_application",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_daily_plan_row_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "plan_id", "position", name="uq_ai_daily_plan_row_position"
        ),
    )
    op.create_index(
        "ix_ai_daily_plan_row_day",
        "ai_daily_plan_row",
        ["tenant_id", "optimizer_id", "business_date", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_daily_plan_row_day", table_name="ai_daily_plan_row")
    op.drop_table("ai_daily_plan_row")
    op.drop_index("ix_ai_daily_plan_optimizer_day", table_name="ai_daily_plan")
    op.drop_table("ai_daily_plan")

