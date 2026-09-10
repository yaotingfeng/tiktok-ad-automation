"""Preserve each signed permission and its direct transport completion evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "r2_part_receipts"
down_revision = "r2_ingest_chunks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "original_use",
        sa.Column(
            "completion_evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("original_use", "completion_evidence", server_default=None)


def downgrade():
    op.drop_column("original_use", "completion_evidence")
