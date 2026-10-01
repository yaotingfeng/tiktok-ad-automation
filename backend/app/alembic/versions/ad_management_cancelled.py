"""Allow durable cancellation as an item terminal result (C6)."""

from alembic import op

revision = "ad_management_cancelled"
down_revision = "ad_management_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, constraint in (
        ("management_preview_item", "ck_management_preview_item_result"),
        ("management_task_item", "ck_management_task_item_result"),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            "execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED','CANCELLED')",
        )


def downgrade() -> None:
    for table, constraint in (
        ("management_preview_item", "ck_management_preview_item_result"),
        ("management_task_item", "ck_management_task_item_result"),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            "execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED')",
        )
