"""Keep expansion priority and fair-yield probes independent of unit fanout."""

import sqlalchemy as sa
from alembic import op

revision = "0013_dispatch_expansion_index"
down_revision = "0012_material_covers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_dispatch_expansion_pending",
        "pending_dispatch",
        ["tenant_id", "available_at", "id"],
        postgresql_where=sa.text(
            "published_at IS NULL AND task_name = 'builds.expand_submission'"
        ),
    )


def downgrade():
    op.drop_index("ix_dispatch_expansion_pending", table_name="pending_dispatch")
