"""Persist direct management intent independently of expiring previews."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068_direct_management"
down_revision = "0067_merge_directory_turn"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_management_attempt_item_id",
        "management_request_attempt",
        ["tenant_id", "task_item_id", "id"],
    )
    op.create_unique_constraint(
        "uq_management_task_item_task_id",
        "management_task_item",
        ["tenant_id", "task_id", "id"],
    )
    op.add_column(
        "management_task",
        sa.Column(
            "source_kind", sa.String(16), nullable=False, server_default="PREVIEW"
        ),
    )
    op.add_column(
        "management_task",
        sa.Column("request", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "management_task",
        sa.Column("prepare_cursor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("management_task", "preview_id", nullable=True)
    op.add_column(
        "management_task_item",
        sa.Column(
            "source_kind", sa.String(16), nullable=False, server_default="PREVIEW"
        ),
    )
    op.alter_column("management_task_item", "preview_id", nullable=True)
    op.alter_column("management_task_item", "preview_item_id", nullable=True)
    op.create_unique_constraint(
        "uq_management_task_source",
        "management_task",
        ["tenant_id", "id", "source_kind"],
    )
    op.create_foreign_key(
        "fk_management_item_source",
        "management_task_item",
        "management_task",
        ["tenant_id", "task_id", "source_kind"],
        ["tenant_id", "id", "source_kind"],
    )
    op.create_check_constraint(
        "ck_management_task_source",
        "management_task",
        "(source_kind = 'PREVIEW' AND preview_id IS NOT NULL) OR (source_kind = 'DIRECT' AND preview_id IS NULL AND request ? 'mutation' AND request ? 'selection')",
    )
    op.create_check_constraint(
        "ck_management_item_source",
        "management_task_item",
        "(source_kind = 'PREVIEW' AND preview_id IS NOT NULL AND preview_item_id IS NOT NULL) OR (source_kind = 'DIRECT' AND preview_id IS NULL AND preview_item_id IS NULL)",
    )
    op.create_index(
        "ix_management_item_position",
        "management_task_item",
        ["tenant_id", "task_id", "position"],
    )


def downgrade():
    # 已有直接任务不能降级为伪预览；保留证据，明确阻止破坏性降级。
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM management_task WHERE source_kind='DIRECT' LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("direct management tasks prevent downgrade")
    op.drop_constraint("uq_management_attempt_item_id", "management_request_attempt")
    op.drop_constraint("uq_management_task_item_task_id", "management_task_item")
    op.drop_index("ix_management_item_position", table_name="management_task_item")
    op.drop_constraint("ck_management_item_source", "management_task_item")
    op.drop_constraint("ck_management_task_source", "management_task")
    op.drop_constraint("fk_management_item_source", "management_task_item")
    op.drop_constraint("uq_management_task_source", "management_task")
    for col in ("preview_id", "preview_item_id"):
        op.alter_column("management_task_item", col, nullable=False)
    op.alter_column("management_task", "preview_id", nullable=False)
    op.drop_column("management_task_item", "source_kind")
    for col in ("source_kind", "request", "prepare_cursor"):
        op.drop_column("management_task", col)
