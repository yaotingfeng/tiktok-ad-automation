"""Fence automatic rules to optimizer scopes and retain decision evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0122_ai_workbench_rule_bindings"
down_revision = "0121_ai_build_occurrences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("automation_rule_run", sa.Column("optimizer_id", sa.Uuid(), nullable=True))
    op.add_column("automation_rule_run", sa.Column("optimizer_version", sa.Integer(), nullable=True))
    op.add_column("automation_rule_run", sa.Column("binding_version", sa.Integer(), nullable=True))
    op.add_column("automation_rule_run", sa.Column("binding_id", sa.Uuid(), nullable=True))
    op.add_column("automation_rule_run", sa.Column("authorization_revision", sa.Integer(), nullable=True))
    op.drop_constraint("uq_automation_run_slot", "automation_rule_run", type_="unique")
    op.create_unique_constraint(
        "uq_automation_run_slot", "automation_rule_run", ["rule_id", "slot_at", "optimizer_id"]
    )
    op.create_foreign_key(
        "fk_automation_run_optimizer", "automation_rule_run", "ai_optimizer",
        ["tenant_id", "optimizer_id"], ["tenant_id", "id"],
    )

    op.create_table(
        "automation_rule_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=True),
        sa.Column("optimizer_version", sa.Integer(), nullable=True),
        sa.Column("binding_version", sa.Integer(), nullable=True),
        sa.Column("binding_id", sa.Uuid(), nullable=True),
        sa.Column("bc_id", sa.String(length=128), nullable=True),
        sa.Column("advertiser_ids", JSONB(), nullable=False),
        sa.Column("authorization_revision", sa.Integer(), nullable=True),
        sa.Column("checked_metrics", JSONB(), nullable=False),
        sa.Column("data_freshness", JSONB(), nullable=False),
        sa.Column("publication_versions", JSONB(), nullable=False),
        sa.Column("skipped_accounts", JSONB(), nullable=False),
        sa.Column("checked_objects", JSONB(), nullable=False),
        sa.Column("hits", JSONB(), nullable=False),
        sa.Column("action_ledger_refs", JSONB(), nullable=False),
        sa.Column("management_tasks", JSONB(), nullable=False),
        sa.Column("readback_status", JSONB(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["automation_rule_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["automation_rule.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"], ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_automation_evidence_optimizer",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_rule_evidence_tenant_created", "automation_rule_evidence", ["tenant_id", "created_at"])
    op.create_index("ix_automation_rule_evidence_retention", "automation_rule_evidence", ["retention_until"])
    op.add_column("automation_rule_run", sa.Column("evidence_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_automation_run_evidence", "automation_rule_run", "automation_rule_evidence",
        ["evidence_id"], ["id"], ondelete="SET NULL",
    )

    op.add_column("automation_action_ledger", sa.Column("optimizer_id", sa.Uuid(), nullable=True))
    op.add_column("automation_action_ledger", sa.Column("rule_version", sa.Integer(), nullable=True))
    op.add_column("automation_action_ledger", sa.Column("evidence_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_automation_ledger_evidence", "automation_action_ledger", "automation_rule_evidence",
        ["evidence_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_automation_ledger_optimizer", "automation_action_ledger", "ai_optimizer",
        ["tenant_id", "optimizer_id"], ["tenant_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_automation_ledger_optimizer", "automation_action_ledger", type_="foreignkey")
    op.drop_constraint("fk_automation_ledger_evidence", "automation_action_ledger", type_="foreignkey")
    op.drop_column("automation_action_ledger", "evidence_id")
    op.drop_column("automation_action_ledger", "rule_version")
    op.drop_column("automation_action_ledger", "optimizer_id")
    op.drop_constraint("fk_automation_run_evidence", "automation_rule_run", type_="foreignkey")
    op.drop_column("automation_rule_run", "evidence_id")
    op.drop_index("ix_automation_rule_evidence_retention", table_name="automation_rule_evidence")
    op.drop_index("ix_automation_rule_evidence_tenant_created", table_name="automation_rule_evidence")
    op.drop_table("automation_rule_evidence")
    op.drop_constraint("fk_automation_run_optimizer", "automation_rule_run", type_="foreignkey")
    op.drop_constraint("uq_automation_run_slot", "automation_rule_run", type_="unique")
    op.create_unique_constraint("uq_automation_run_slot", "automation_rule_run", ["rule_id", "slot_at"])
    op.drop_column("automation_rule_run", "authorization_revision")
    op.drop_column("automation_rule_run", "binding_id")
    op.drop_column("automation_rule_run", "binding_version")
    op.drop_column("automation_rule_run", "optimizer_version")
    op.drop_column("automation_rule_run", "optimizer_id")
