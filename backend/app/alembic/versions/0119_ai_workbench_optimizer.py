"""Tenant-scoped AI optimizer identities, immutable versions and bindings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0119_ai_workbench_optimizer"
down_revision = "0118_sync_schedule_service_age"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_optimizer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_tenant_id"),
    )
    op.create_index("ix_ai_optimizer_tenant", "ai_optimizer", ["tenant_id", "created_at"])

    op.create_table(
        "ai_optimizer_strategy_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_binding_optimizer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "strategy_version_id"],
            ["strategy_version.tenant_id", "strategy_version.id"],
            name="fk_ai_binding_strategy_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "optimizer_id", "id", name="uq_ai_binding_tenant_optimizer_id"
        ),
    )
    op.create_index(
        "uq_ai_binding_one_active",
        "ai_optimizer_strategy_binding",
        ["tenant_id", "optimizer_id"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.create_index(
        "ix_ai_binding_optimizer",
        "ai_optimizer_strategy_binding",
        ["tenant_id", "optimizer_id", "created_at"],
    )

    op.create_table(
        "ai_optimizer_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("daily_spend", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column("target_roas", sa.Numeric(precision=38, scale=12), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("advertiser_ids", JSONB(), nullable=False),
        sa.Column("optimization_mode", sa.String(length=16), nullable=False),
        sa.Column("authorization_limits", JSONB(), nullable=False),
        sa.Column("strategy_binding_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_version_optimizer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id", "strategy_binding_id"],
            [
                "ai_optimizer_strategy_binding.tenant_id",
                "ai_optimizer_strategy_binding.optimizer_id",
                "ai_optimizer_strategy_binding.id",
            ],
            name="fk_ai_version_binding",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_ai_optimizer_version_tenant_id"),
        sa.UniqueConstraint("tenant_id", "optimizer_id", "number", name="uq_ai_optimizer_version_number"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_ai_optimizer_version_request"),
        sa.CheckConstraint(
            "number > 0 AND daily_spend > 0 AND (target_roas IS NULL OR target_roas > 0)",
            name="ck_ai_optimizer_version_values",
        ),
        sa.CheckConstraint("priority IN ('SPEND','ROAS','BALANCED')", name="ck_ai_optimizer_priority"),
        sa.CheckConstraint("optimization_mode IN ('AUTO','MANUAL')", name="ck_ai_optimizer_mode"),
    )
    op.create_index(
        "ix_ai_optimizer_version_optimizer",
        "ai_optimizer_version",
        ["tenant_id", "optimizer_id", "number"],
    )

    op.create_table(
        "ai_optimizer_rule_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["rule_id"], ["automation_rule.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_rule_binding_optimizer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "optimizer_id", "rule_id", name="uq_ai_optimizer_rule"),
    )
    op.create_index(
        "ix_ai_rule_binding_optimizer",
        "ai_optimizer_rule_binding",
        ["tenant_id", "optimizer_id", "active"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_rule_binding_optimizer", table_name="ai_optimizer_rule_binding")
    op.drop_table("ai_optimizer_rule_binding")
    op.drop_index("ix_ai_optimizer_version_optimizer", table_name="ai_optimizer_version")
    op.drop_table("ai_optimizer_version")
    op.drop_index("ix_ai_binding_optimizer", table_name="ai_optimizer_strategy_binding")
    op.drop_index("uq_ai_binding_one_active", table_name="ai_optimizer_strategy_binding")
    op.drop_table("ai_optimizer_strategy_binding")
    op.drop_index("ix_ai_optimizer_tenant", table_name="ai_optimizer")
    op.drop_table("ai_optimizer")
