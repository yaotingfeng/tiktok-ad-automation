"""Preserve actual VIDEO partial-failure receipts without inventing old evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "material_share_receipts"
down_revision = "material_cover_sharing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "material_share_batch_receipt",
        sa.Column("share_response", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.execute("LOCK TABLE material_share_batch_receipt IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM material_share_batch_receipt WHERE share_response IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot drop actual material share responses';
        END IF;
    END $$""")
    op.drop_column("material_share_batch_receipt", "share_response")
