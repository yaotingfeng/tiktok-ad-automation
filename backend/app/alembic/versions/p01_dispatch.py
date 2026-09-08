"""Persist dispatch and tenant scheduling cursors.

Revision ID: p01_dispatch
Revises: fe56fa70289e
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "p01_dispatch"
down_revision = "fe56fa70289e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dispatch_tenant_cursor",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_table(
        "pending_dispatch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("task_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "task_key", name="uq_dispatch_tenant_key"),
    )
    op.create_index("ix_dispatch_pending", "pending_dispatch", ["available_at", "id"], postgresql_where=sa.text("published_at IS NULL"))
    op.create_index("ix_dispatch_tenant_pending", "pending_dispatch", ["tenant_id", "available_at", "id"], postgresql_where=sa.text("published_at IS NULL"))


def downgrade():
    op.drop_table("pending_dispatch")
    op.drop_table("dispatch_tenant_cursor")
