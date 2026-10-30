"""Persist physical management requests and exact members.

Revision ID: 0069_management_batches
Revises: 0068_direct_management
"""

import sqlalchemy as sa
from alembic import op

revision = "0069_management_batches"
down_revision = "0068_direct_management"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "management_physical_request",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "id", name="uq_management_physical_scope"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["management_task.tenant_id", "management_task.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("member_count >= 1", name="ck_management_physical_size"),
        sa.CheckConstraint(
            "status IN ('RECORDED','COMPLETE','UNKNOWN')",
            name="ck_management_physical_status",
        ),
    )
    op.create_index(
        "ix_management_physical_task",
        "management_physical_request",
        ["tenant_id", "task_id", "created_at"],
    )
    op.create_table(
        "management_physical_member",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("task_item_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "batch_id"],
            [
                "management_physical_request.tenant_id",
                "management_physical_request.task_id",
                "management_physical_request.id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "task_item_id"],
            [
                "management_task_item.tenant_id",
                "management_task_item.task_id",
                "management_task_item.id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_item_id", "attempt_id"],
            [
                "management_request_attempt.tenant_id",
                "management_request_attempt.task_item_id",
                "management_request_attempt.id",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("attempt_id", name="uq_management_physical_attempt"),
        sa.UniqueConstraint(
            "batch_id", "task_item_id", name="uq_management_physical_member"
        ),
    )


def downgrade():
    op.drop_table("management_physical_member")
    op.drop_index(
        "ix_management_physical_task", table_name="management_physical_request"
    )
    op.drop_table("management_physical_request")
