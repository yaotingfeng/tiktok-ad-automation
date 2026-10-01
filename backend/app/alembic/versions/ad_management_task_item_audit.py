"""Persist immutable preview audit fields on management task items."""

import sqlalchemy as sa
from alembic import op

revision = "ad_management_task_item_audit"
down_revision = "ad_management_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "management_task_item",
        sa.Column("reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "management_task_item",
        sa.Column("membership_digest", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("management_task_item", "membership_digest")
    op.drop_column("management_task_item", "reason")
