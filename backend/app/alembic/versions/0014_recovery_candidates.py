"""Find recovery candidates without scanning every pending execution step."""

import sqlalchemy as sa
from alembic import op

revision = "0014_recovery_candidates"
down_revision = "0013_dispatch_expansion_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_recovery_candidates",
        "execution_step",
        ["tenant_id", "submission_id", "id"],
        postgresql_where=sa.text(
            "dispatch_id IS NULL AND (status='UNKNOWN' OR mismatch OR (kind='MATERIAL' AND status='FAILED'))"
        ),
    )
    op.create_index(
        "ix_recovery_known_parent",
        "execution_step",
        ["tenant_id", "submission_id", "id"],
        postgresql_where=sa.text("remote_id IS NOT NULL"),
    )
    op.create_index(
        "ix_recovery_readback_parent",
        "execution_step",
        ["tenant_id", "submission_id", "parent_step_id", "id"],
        postgresql_where=sa.text(
            "dispatch_id IS NULL AND kind='READBACK' AND status<>'SUCCEEDED'"
        ),
    )


def downgrade():
    op.drop_index("ix_recovery_readback_parent", table_name="execution_step")
    op.drop_index("ix_recovery_known_parent", table_name="execution_step")
    op.drop_index("ix_recovery_candidates", table_name="execution_step")
