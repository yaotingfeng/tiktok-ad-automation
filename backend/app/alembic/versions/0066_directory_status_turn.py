"""持久保存目录缺口与普通刷新之间的派发轮次。"""

import sqlalchemy as sa
from alembic import op

revision = "0066_directory_status_turn"
down_revision = "0064_closeout_job_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 仅租户游标小表加可空列，无默认值、无业务表重写或历史任务改写。
    op.add_column(
        "directory_dispatch_cursor",
        sa.Column("last_status_missing", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("directory_dispatch_cursor", "last_status_missing")
