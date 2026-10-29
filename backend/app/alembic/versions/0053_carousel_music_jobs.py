"""Durable scoped music selection jobs.

Revision ID: 0053_carousel_music
Revises: 0052_image_candidate_build
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053_carousel_music"
down_revision = "0052_image_candidate_build"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "music_selection_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("active_generation", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("route", postgresql.JSONB(), nullable=False),
        sa.Column("seed_advertiser_id", sa.String(128)),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("material_ids", postgresql.JSONB(), nullable=False),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("wait_turns", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("claim", sa.Uuid()),
        sa.Column("dispatch_id", sa.Uuid(), sa.ForeignKey("pending_dispatch.id")),
        sa.Column("repair_after", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "active_generation",
            "fingerprint",
            name="uq_music_job_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','READY','FAILED','CANCELLED')",
            name="ck_music_job_status",
        ),
        sa.CheckConstraint(
            "active_generation > 0 AND generation > 0 AND attempts >= 0 AND cursor >= 0 AND cursor <= jsonb_array_length(targets)",
            name="ck_music_job_progress",
        ),
    )
    op.create_index(
        "ix_music_job_repair", "music_selection_job", ["status", "repair_after"]
    )


def downgrade():
    op.drop_table("music_selection_job")
