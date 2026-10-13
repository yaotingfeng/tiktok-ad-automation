"""Automated rules: rule definitions, runs, hits, tenant settings and Feishu webhook."""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_automation_rules"
down_revision = "0025_drop_campaign_day_utc"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _str(length: int) -> sqlmodel.sql.sqltypes.AutoString:
    return sqlmodel.sql.sqltypes.AutoString(length=length)


def upgrade() -> None:
    op.create_table(
        "automation_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", _str(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("next_run_at", _TS, nullable=True),
        sa.Column("last_run_at", _TS, nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_automation_rule_name"),
    )
    op.create_index(
        "ix_automation_rule_due", "automation_rule", ["enabled", "next_run_at"]
    )
    op.create_table(
        "automation_rule_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("slot_at", _TS, nullable=False),
        sa.Column("status", _str(16), nullable=False),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("checked_count", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column(
            "skipped_accounts", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("error_code", _str(64), nullable=True),
        sa.Column("notify_status", _str(16), nullable=False),
        sa.Column("notify_error", _str(500), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETE','FAILED')", name="ck_automation_run_status"
        ),
        sa.CheckConstraint(
            "notify_status IN ('NOT_REQUIRED','PENDING','SENT','FAILED')",
            name="ck_automation_run_notify",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["automation_rule.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "slot_at", name="uq_automation_run_slot"),
    )
    op.create_index(
        "ix_automation_run_tenant_time",
        "automation_rule_run",
        ["tenant_id", "started_at"],
    )
    op.create_table(
        "automation_rule_hit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("subject_key", _str(300), nullable=False),
        sa.Column("subject_label", _str(500), nullable=False),
        sa.Column("drama_name", _str(300), nullable=True),
        sa.Column("advertiser_id", _str(128), nullable=True),
        sa.Column("bc_id", _str(128), nullable=True),
        sa.Column("currency", _str(16), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("outcome", _str(32), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_rule_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_hit_run", "automation_rule_hit", ["run_id", "sequence"]
    )
    op.create_table(
        "automation_tenant_setting",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_timezone", _str(64), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_table(
        "tenant_feishu_setting",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ciphertext", sa.String(), nullable=False),
        sa.Column("url_hint", _str(120), nullable=False),
        sa.Column("has_secret", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("tenant_feishu_setting")
    op.drop_table("automation_tenant_setting")
    op.drop_index("ix_automation_hit_run", table_name="automation_rule_hit")
    op.drop_table("automation_rule_hit")
    op.drop_index("ix_automation_run_tenant_time", table_name="automation_rule_run")
    op.drop_table("automation_rule_run")
    op.drop_index("ix_automation_rule_due", table_name="automation_rule")
    op.drop_table("automation_rule")
