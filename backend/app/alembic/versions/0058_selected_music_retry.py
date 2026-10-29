"""Bound selected-drama retry runs to one user intent and existing recovery.

Revision ID: 0058_selected_music_retry
Revises: 0057_carousel_copy_integrity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0058_selected_music_retry"
down_revision = "0057_carousel_copy_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "music_preparation_run",
        sa.Column("action", sa.String(), nullable=False, server_default="PREPARE"),
    )
    op.add_column(
        "music_preparation_run",
        sa.Column(
            "drama_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "music_preparation_run", sa.Column("base_revision", sa.Integer(), nullable=True)
    )
    for name in ("retried", "skipped_protected", "skipped_recovered"):
        op.add_column(
            "music_preparation_run",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    op.create_check_constraint(
        "ck_music_preparation_action",
        "music_preparation_run",
        "action IN ('PREPARE','RETRY_FAILED') AND (action != 'RETRY_FAILED' OR (base_revision IS NOT NULL AND base_revision > 0 AND jsonb_array_length(drama_ids) BETWEEN 1 AND 100)) AND retried >= 0 AND skipped_protected >= 0 AND skipped_recovered >= 0",
    )


def downgrade():
    op.drop_constraint("ck_music_preparation_action", "music_preparation_run")
    for name in (
        "action",
        "drama_ids",
        "base_revision",
        "retried",
        "skipped_protected",
        "skipped_recovered",
    ):
        op.drop_column("music_preparation_run", name)
