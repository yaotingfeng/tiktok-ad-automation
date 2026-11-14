"""Separate private frozen review sources from initial agent evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0130_ai_review_reading"
down_revision = "0129_ai_proposal_operation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_dsh_review_task",
        sa.Column(
            "evidence_store", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    # 旧任务继续沿用旧版冻结 evidence；不因部署新读取协议而取消正在运行的任务。


def downgrade():
    op.drop_column("ai_dsh_review_task", "evidence_store")
