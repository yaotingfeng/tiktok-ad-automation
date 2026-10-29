"""Bounded durable music preparation scan.

Revision ID: 0054_music_preparation
Revises: 0053_carousel_music
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_music_preparation"
down_revision = "0053_carousel_music"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "music_preparation_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("active_generation", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("route", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("after_id", sa.Uuid()),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("dispatch_id", sa.Uuid(), sa.ForeignKey("pending_dispatch.id")),
        sa.Column("repair_after", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id"],
            ["build_draft.tenant_id", "build_draft.id", "build_draft.bc_id"],
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','READY','FAILED','CANCELLED')",
            name="ck_music_preparation_status",
        ),
        sa.CheckConstraint(
            "active_generation > 0 AND generation > 0 AND processed >= 0",
            name="ck_music_preparation_progress",
        ),
    )
    op.create_index(
        "ix_music_preparation_repair",
        "music_preparation_run",
        ["status", "repair_after"],
    )


def downgrade():
    op.drop_table("music_preparation_run")
