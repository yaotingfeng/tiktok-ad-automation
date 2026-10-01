"""Persist C5 per-item claim fences for management execution."""

import sqlalchemy as sa
from alembic import op

revision = "ad_management_execution"
down_revision = "ad_management_task_item_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "management_task_item",
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("management_task_item", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "management_task_item",
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_management_task_item_claim_generation",
        "management_task_item",
        "claim_generation >= 1",
    )
    op.create_index(
        "ix_management_task_item_claim_due",
        "management_task_item",
        ["tenant_id", "task_id", "claimed_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_management_task_item_claim_due", table_name="management_task_item")
    op.drop_constraint(
        "ck_management_task_item_claim_generation", "management_task_item", type_="check"
    )
    op.drop_column("management_task_item", "claimed_until")
    op.drop_column("management_task_item", "claim_token")
    op.drop_column("management_task_item", "claim_generation")
