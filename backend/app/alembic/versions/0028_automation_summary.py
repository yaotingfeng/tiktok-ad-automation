"""Automated rules fixed-time summary: last summary slot and SUMMARY notify status."""

import sqlalchemy as sa
from alembic import op

revision = "0028_automation_summary"
down_revision = "0027_automation_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automation_rule",
        sa.Column("last_summary_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 汇总模式下运行的通知先置为 SUMMARY，到点合并发送。
    op.drop_constraint("ck_automation_run_notify", "automation_rule_run", type_="check")
    op.create_check_constraint(
        "ck_automation_run_notify",
        "automation_rule_run",
        "notify_status IN ('NOT_REQUIRED','PENDING','SUMMARY','SENT','FAILED')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE automation_rule_run SET notify_status = 'NOT_REQUIRED' "
        "WHERE notify_status = 'SUMMARY'"
    )
    op.drop_constraint("ck_automation_run_notify", "automation_rule_run", type_="check")
    op.create_check_constraint(
        "ck_automation_run_notify",
        "automation_rule_run",
        "notify_status IN ('NOT_REQUIRED','PENDING','SENT','FAILED')",
    )
    op.drop_column("automation_rule", "last_summary_at")
