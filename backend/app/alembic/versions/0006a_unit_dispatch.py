"""Persist exact unit dispatch generations without altering frozen inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0006a_unit_dispatch"
down_revision = "0006_build_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE execution_step DISABLE TRIGGER execution_step_scope")
    op.execute(
        "UPDATE execution_step SET request_body=NULL WHERE request_body='null'::jsonb"
    )
    op.execute("ALTER TABLE execution_step ENABLE TRIGGER execution_step_scope")
    op.create_check_constraint(
        "ck_step_body_object",
        "execution_step",
        "request_body IS NULL OR jsonb_typeof(request_body) = 'object'",
    )
    op.add_column(
        "submission_unit",
        sa.Column(
            "dispatch_revision", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column("submission_unit", sa.Column("dispatch_id", sa.Uuid(), nullable=True))
    op.add_column(
        "submission_unit",
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "submission_unit",
        sa.Column(
            "repair_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    for column in ("dispatch_revision", "due_at", "repair_after"):
        op.alter_column("submission_unit", column, server_default=None)
    op.create_foreign_key(
        "fk_submission_unit_dispatch",
        "submission_unit",
        "pending_dispatch",
        ["dispatch_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_submission_frozen_unit", "submission_unit", ["tenant_id", "unit_id"]
    )
    op.create_check_constraint(
        "ck_unit_dispatch_revision", "submission_unit", "dispatch_revision >= 0"
    )
    op.create_index(
        "ix_unit_dispatch_repair",
        "submission_unit",
        ["expanded", "repair_after", "unit_id"],
    )


def downgrade():
    op.drop_constraint("ck_step_body_object", "execution_step", type_="check")
    op.drop_index("ix_unit_dispatch_repair", table_name="submission_unit")
    op.drop_constraint("ck_unit_dispatch_revision", "submission_unit", type_="check")
    op.drop_constraint("uq_submission_frozen_unit", "submission_unit", type_="unique")
    op.drop_constraint(
        "fk_submission_unit_dispatch", "submission_unit", type_="foreignkey"
    )
    for column in ("repair_after", "due_at", "dispatch_id", "dispatch_revision"):
        op.drop_column("submission_unit", column)
