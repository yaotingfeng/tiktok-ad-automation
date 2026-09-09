"""Bound historical send-evidence lookup within each execution step."""

from alembic import op

revision = "0011_recovery_evidence_index"
down_revision = "0010_submission_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_step_evidence_identity",
        "step_evidence",
        ["tenant_id", "submission_id", "step_id"],
    )


def downgrade():
    op.drop_index("ix_step_evidence_identity", table_name="step_evidence")
