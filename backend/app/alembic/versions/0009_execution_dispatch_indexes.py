"""Index pending work within each large submitted scope."""

import sqlalchemy as sa
from alembic import op

revision = "0009_execution_dispatch_indexes"
down_revision = "0008_scene_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_submission_unit_dispatch",
        "submission_unit",
        ["tenant_id", "submission_id", "unit_id"],
        postgresql_where=sa.text(
            "disposition = 'INCLUDED' AND (expanded = false OR dispatch_id IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_execution_scope_status",
        "execution_step",
        ["tenant_id", "submission_id", "status"],
    )
    op.create_index(
        "ix_execution_unit_due",
        "execution_step",
        ["tenant_id", "submission_id", "unit_id", "due_at", "id"],
        postgresql_where=sa.text(
            "status IN ('PENDING','RETRYABLE','UNKNOWN','RUNNING')"
        ),
    )


def downgrade():
    op.drop_index("ix_execution_unit_due", table_name="execution_step")
    op.drop_index("ix_execution_scope_status", table_name="execution_step")
    op.drop_index("ix_submission_unit_dispatch", table_name="submission_unit")
