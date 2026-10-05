"""Restore the preview copy uniqueness constraint declared by the model."""

from alembic import op

revision = "20261005_preview_copy_unique"
down_revision = "20261005_merge_preview_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_preview_copy_no",
        "preview_copy",
        ["tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "creative_no"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_preview_copy_no", "preview_copy", type_="unique")
