"""Persist bounded claims around object-storage network operations.

Revision ID: 0004c_object_claims
Revises: 0004b_material_checks
"""

import sqlalchemy as sa
from alembic import op

revision = "0004c_object_claims"
down_revision = "0004b_material_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("object_upload", sa.Column("attempt_token", sa.Uuid(), nullable=True))
    op.add_column(
        "object_upload",
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("object_upload", "claimed_until")
    op.drop_column("object_upload", "attempt_token")
