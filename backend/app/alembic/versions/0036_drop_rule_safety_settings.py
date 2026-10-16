"""移除自动规则的反向冷却期与每日涨幅上限设置。

反向冲突改为判断对方规则按当前数据是否仍命中；涨幅由规则动作必填的上限与执行频率约束。
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_drop_rule_safety_settings"
down_revision = "0035_report_dispatch_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("automation_tenant_setting", "daily_increase_limit_pct")
    op.drop_column("automation_tenant_setting", "reverse_cooldown_hours")


def downgrade() -> None:
    # 回退时按原默认值补回：24 小时冷却、日涨幅 100%。
    op.add_column(
        "automation_tenant_setting",
        sa.Column(
            "reverse_cooldown_hours", sa.Integer(), nullable=False, server_default="24"
        ),
    )
    op.add_column(
        "automation_tenant_setting",
        sa.Column(
            "daily_increase_limit_pct",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    )
    for column in ("reverse_cooldown_hours", "daily_increase_limit_pct"):
        op.alter_column("automation_tenant_setting", column, server_default=None)
