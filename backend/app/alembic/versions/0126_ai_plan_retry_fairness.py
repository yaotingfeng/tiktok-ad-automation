"""Persist bounded dispatch retry watermarks and blocked reasons."""

import sqlalchemy as sa
from alembic import op

revision = "0126_ai_plan_retry_fairness"
down_revision = "0125_ai_material_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_daily_plan_row",
        sa.Column(
            "next_dispatch_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00+00'::timestamptz"),
        ),
    )
    op.add_column(
        "ai_daily_plan_row",
        sa.Column("last_dispatch_error", sa.String(128), nullable=True),
    )
    op.drop_index("ix_ai_daily_plan_row_dispatch", table_name="ai_daily_plan_row")
    op.create_index(
        "ix_ai_plan_retry_dispatch",
        "ai_daily_plan_row",
        ["next_dispatch_at", "business_date", "scheduled_time", "id"],
    )


def downgrade():
    op.drop_index("ix_ai_plan_retry_dispatch", table_name="ai_daily_plan_row")
    op.create_index(
        "ix_ai_daily_plan_row_dispatch",
        "ai_daily_plan_row",
        ["business_date", "scheduled_time", "id"],
    )
    op.drop_column("ai_daily_plan_row", "last_dispatch_error")
    op.drop_column("ai_daily_plan_row", "next_dispatch_at")
