"""有界报表窗口使用独立持久游标，确保连续单槽补位仍跨租户轮转。"""

import sqlalchemy as sa
from alembic import op

revision = "0035_report_dispatch_cursor"
down_revision = "0034_directory_publish_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_dispatch_cursor",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("report_dispatch_cursor")
