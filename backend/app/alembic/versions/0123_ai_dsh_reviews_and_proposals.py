"""Persist DSH review tasks and optimizer publication proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0123_ai_dsh_reviews_and_proposals"
down_revision = "0122_ai_workbench_rule_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_dsh_review_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("dsh_task_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("authorization_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fence", sa.String(length=64), nullable=False),
        sa.Column("restart_generation", sa.Integer(), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("optimizer_version", sa.Integer(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_dsh_review_optimizer",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_ai_dsh_review_request"),
    )
    op.create_index(
        "ix_ai_dsh_review_optimizer_created",
        "ai_dsh_review_task",
        ["tenant_id", "optimizer_id", "created_at"],
    )

    op.create_table(
        "ai_optimizer_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("optimizer_id", sa.Uuid(), nullable=False),
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("before", JSONB(), nullable=False),
        sa.Column("after", JSONB(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("authorization", JSONB(), nullable=False),
        sa.Column("base_optimizer_version", sa.Integer(), nullable=False),
        sa.Column("base_strategy_version_id", sa.Uuid(), nullable=True),
        sa.Column("base_rule_version", sa.Integer(), nullable=True),
        sa.Column("base_rule_id", sa.Uuid(), nullable=True),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("decision_by", sa.Uuid(), nullable=True),
        sa.Column("decision_reason", sa.String(length=1000), nullable=True),
        sa.Column("decision_request_id", sa.Uuid(), nullable=True),
        sa.Column("decision_request_digest", sa.String(length=64), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_proposal_optimizer",
        ),
        sa.ForeignKeyConstraint(["review_task_id"], ["ai_dsh_review_task.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["decision_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_ai_proposal_request"),
        sa.CheckConstraint("kind IN ('strategy','rule')", name="ck_ai_proposal_kind"),
        sa.CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED','STALE')",
            name="ck_ai_proposal_status",
        ),
    )
    op.create_index(
        "ix_ai_proposal_optimizer_status",
        "ai_optimizer_proposal",
        ["tenant_id", "optimizer_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_proposal_optimizer_status", table_name="ai_optimizer_proposal")
    op.drop_table("ai_optimizer_proposal")
    op.drop_index("ix_ai_dsh_review_optimizer_created", table_name="ai_dsh_review_task")
    op.drop_table("ai_dsh_review_task")
