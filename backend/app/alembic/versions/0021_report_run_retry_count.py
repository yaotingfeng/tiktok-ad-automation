"""Track bounded provider retries on report sync runs."""

import sqlalchemy as sa
from alembic import op

revision = "0021_report_run_retry_count"
down_revision = "0020_draft_delivery_mutation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 平台读取失败的有界重试次数；额度/本地资源延后不计入。
    op.add_column(
        "report_sync_run",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("report_sync_run", "retry_count")
