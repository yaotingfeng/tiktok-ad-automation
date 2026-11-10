"""Add tenant scoped campaign copy drafts and durable request identities."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0110_campaign_copy"
down_revision = "0109_snapshot_inline_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_copy_draft",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("route", postgresql.JSONB(), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.Column("children", postgresql.JSONB(), nullable=False),
        sa.Column("preview_digest", sa.String(64)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), sa.ForeignKey("pending_dispatch.id")),
        sa.Column("repair_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_copy_scope"),
        sa.ForeignKeyConstraint(["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]),
        sa.CheckConstraint("revision > 0 AND generation >= 0", name="ck_campaign_copy_revision"),
        sa.CheckConstraint("status IN ('DRAFT','PREPARING','READY','SUBMITTED','FAILED')", name="ck_campaign_copy_status"),
    )
    op.create_index("ix_campaign_copy_repair", "campaign_copy_draft", ["status", "repair_after", "id"])
    op.create_table(
        "campaign_copy_request",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "draft_id"], ["campaign_copy_draft.tenant_id", "campaign_copy_draft.id"]),
    )


def downgrade() -> None:
    op.drop_table("campaign_copy_request")
    op.drop_index("ix_campaign_copy_repair", table_name="campaign_copy_draft")
    op.drop_table("campaign_copy_draft")
