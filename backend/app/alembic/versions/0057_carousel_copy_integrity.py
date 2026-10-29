"""Reject half-populated copy receipts despite SQL NULL check semantics.

Revision ID: 0057_carousel_copy_integrity
Revises: 0056_carousel_preset_copy
"""

from alembic import op

revision = "0057_carousel_copy_integrity"
down_revision = "0056_carousel_preset_copy"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_carousel_preset_copy_request", "carousel_preset_version")
    op.create_check_constraint(
        "ck_carousel_preset_copy_request",
        "carousel_preset_version",
        "(copy_request_id IS NULL AND copy_request_digest IS NULL) OR (copy_request_id IS NOT NULL AND copy_request_digest IS NOT NULL AND length(copy_request_digest) = 64)",
    )


def downgrade():
    op.drop_constraint("ck_carousel_preset_copy_request", "carousel_preset_version")
    op.create_check_constraint(
        "ck_carousel_preset_copy_request",
        "carousel_preset_version",
        "(copy_request_id IS NULL AND copy_request_digest IS NULL) OR (copy_request_id IS NOT NULL AND length(copy_request_digest) = 64)",
    )
