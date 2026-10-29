"""Tenant-only library copy receipts, independent of draft routes.

Revision ID: 0056_carousel_preset_copy
Revises: 0055_carousel_presets
"""

import sqlalchemy as sa
from alembic import op

revision = "0056_carousel_preset_copy"
down_revision = "0055_carousel_presets"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("carousel_preset_version", sa.Column("copy_request_id", sa.Uuid()))
    op.add_column(
        "carousel_preset_version", sa.Column("copy_request_digest", sa.String(64))
    )
    op.create_unique_constraint(
        "uq_carousel_preset_copy_request",
        "carousel_preset_version",
        ["tenant_id", "copy_request_id"],
    )
    op.create_check_constraint(
        "ck_carousel_preset_copy_request",
        "carousel_preset_version",
        "(copy_request_id IS NULL AND copy_request_digest IS NULL) OR (copy_request_id IS NOT NULL AND length(copy_request_digest) = 64)",
    )


def downgrade():
    op.drop_constraint("ck_carousel_preset_copy_request", "carousel_preset_version")
    op.drop_constraint("uq_carousel_preset_copy_request", "carousel_preset_version")
    op.drop_column("carousel_preset_version", "copy_request_digest")
    op.drop_column("carousel_preset_version", "copy_request_id")
