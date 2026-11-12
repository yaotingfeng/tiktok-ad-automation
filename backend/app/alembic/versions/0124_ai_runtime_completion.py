"""Complete optimizer wall-clock scheduling contracts."""

import sqlalchemy as sa
from alembic import op

revision = "0124_ai_runtime_completion"
down_revision = "0123_ai_dsh_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_optimizer_version",
        sa.Column(
            "timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"
        ),
    )
    op.add_column(
        "ai_optimizer_version",
        sa.Column("review_time", sa.String(5), nullable=False, server_default="13:00"),
    )
    op.add_column(
        "ai_daily_plan_row",
        sa.Column(
            "timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"
        ),
    )


def downgrade():
    op.drop_column("ai_daily_plan_row", "timezone")
    op.drop_column("ai_optimizer_version", "review_time")
    op.drop_column("ai_optimizer_version", "timezone")
