"""Automated rules execution: action ledger, confirmations, run execution state."""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_automation_execution"
down_revision = "0026_automation_rules"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _str(length: int) -> sqlmodel.sql.sqltypes.AutoString:
    return sqlmodel.sql.sqltypes.AutoString(length=length)


def upgrade() -> None:
    # 已有运行与设置行按阶段 1 语义补默认值：仅通知、无需执行、24 小时冷却、日涨幅 100%。
    op.add_column(
        "automation_rule_run",
        sa.Column("mode", _str(16), nullable=False, server_default="NOTIFY"),
    )
    op.add_column(
        "automation_rule_run",
        sa.Column(
            "execution_status", _str(24), nullable=False, server_default="NOT_REQUIRED"
        ),
    )
    op.add_column(
        "automation_rule_run",
        sa.Column(
            "execution_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
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
    for table, column in (
        ("automation_rule_run", "mode"),
        ("automation_rule_run", "execution_status"),
        ("automation_rule_run", "execution_summary"),
        ("automation_tenant_setting", "reverse_cooldown_hours"),
        ("automation_tenant_setting", "daily_increase_limit_pct"),
    ):
        op.alter_column(table, column, server_default=None)
    op.create_table(
        "automation_action_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("target_key", _str(300), nullable=False),
        sa.Column("action", _str(32), nullable=False),
        sa.Column("original_value", _str(128), nullable=True),
        sa.Column("final_value", _str(128), nullable=True),
        sa.Column("management_task_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["automation_rule.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_rule_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_ledger_target",
        "automation_action_ledger",
        ["tenant_id", "target_key", "created_at"],
    )
    op.create_table(
        "automation_confirmation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", _str(16), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','CONFIRMED','DISMISSED','EXPIRED')",
            name="ck_automation_confirmation_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["automation_rule.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_rule_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_automation_confirmation_run"),
    )
    op.create_index(
        "ix_automation_confirmation_status",
        "automation_confirmation",
        ["tenant_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_confirmation_status", table_name="automation_confirmation"
    )
    op.drop_table("automation_confirmation")
    op.drop_index("ix_automation_ledger_target", table_name="automation_action_ledger")
    op.drop_table("automation_action_ledger")
    op.drop_column("automation_tenant_setting", "daily_increase_limit_pct")
    op.drop_column("automation_tenant_setting", "reverse_cooldown_hours")
    op.drop_column("automation_rule_run", "execution_summary")
    op.drop_column("automation_rule_run", "execution_status")
    op.drop_column("automation_rule_run", "mode")
