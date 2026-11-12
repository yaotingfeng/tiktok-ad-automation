"""Versioned public knowledge and tenant/BC private experience."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0127_ai_knowledge"
down_revision = "0126_ai_plan_retry_fairness"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_knowledge_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id")),
        sa.Column("bc_id", sa.String(128)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("source", sa.String(500), nullable=False),
        sa.Column("source_verified", sa.Boolean(), nullable=False),
        sa.Column("applicability", sa.String(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_ids", postgresql.JSONB(), nullable=False),
        sa.Column("conflict_key", sa.String(100)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("sharing_review", sa.String()),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("asset_id", "version", name="uq_ai_knowledge_version"),
        sa.CheckConstraint(
            "scope IN ('PUBLIC','TENANT','BC')", name="ck_ai_knowledge_scope"
        ),
        sa.CheckConstraint(
            "status IN ('CONFIRMED','CANDIDATE','RETIRED')",
            name="ck_ai_knowledge_status",
        ),
        sa.CheckConstraint(
            "(scope = 'PUBLIC' AND tenant_id IS NULL AND bc_id IS NULL) OR (scope = 'TENANT' AND tenant_id IS NOT NULL AND bc_id IS NULL) OR (scope = 'BC' AND tenant_id IS NOT NULL AND bc_id IS NOT NULL)",
            name="ck_ai_knowledge_owner",
        ),
    )
    op.create_index(
        "ix_ai_knowledge_scope",
        "ai_knowledge_version",
        ["tenant_id", "bc_id", "scope", "created_at"],
    )


def downgrade():
    op.drop_table("ai_knowledge_version")
