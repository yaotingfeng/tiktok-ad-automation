"""Record whether an AI proposal creates or updates a domain object."""

import sqlalchemy as sa
from alembic import op

revision = "0129_ai_proposal_operation"
down_revision = "0128_material_production_origin"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_optimizer_proposal",
        sa.Column(
            "operation", sa.String(length=16), nullable=False, server_default="update"
        ),
    )
    op.create_check_constraint(
        "ck_ai_proposal_operation",
        "ai_optimizer_proposal",
        "operation IN ('create','update')",
    )
    # 发布权限语义已改变；旧方法包/旧授权快照必须重新复盘，不能扩大旧提案权限。
    op.execute(
        "UPDATE ai_optimizer_proposal SET status='STALE', decision_reason='publication_contract_upgraded' WHERE status='PENDING'"
    )
    op.execute(
        "UPDATE ai_dsh_review_task SET status='CANCELLED', cancellation_requested=true, finished_at=CURRENT_TIMESTAMP WHERE status IN ('PENDING','RUNNING')"
    )


def downgrade():
    op.drop_constraint(
        "ck_ai_proposal_operation", "ai_optimizer_proposal", type_="check"
    )
    op.drop_column("ai_optimizer_proposal", "operation")
