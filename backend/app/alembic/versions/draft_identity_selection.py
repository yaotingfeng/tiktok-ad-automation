"""Persist one batch-level TikTok identity selection."""

import sqlalchemy as sa
from alembic import op

revision = "draft_identity_selection"
down_revision = "draft_scene_phase"
branch_labels = None
depends_on = None


def upgrade():
    for name, length in (
        ("identity_id", 255),
        ("identity_type", 32),
        ("identity_authorized_bc_id", 255),
        ("identity_display_name", 255),
        ("identity_username", 255),
    ):
        op.add_column(
            "build_draft", sa.Column(name, sa.String(length=length), nullable=True)
        )
    op.create_check_constraint(
        "ck_build_draft_identity",
        "build_draft",
        "(identity_id IS NULL AND identity_type IS NULL AND identity_authorized_bc_id IS NULL) "
        "OR (identity_id IS NOT NULL AND identity_type IN ('TT_USER','BC_AUTH_TT') "
        "AND ((identity_type='TT_USER' AND identity_authorized_bc_id IS NULL) "
        "OR (identity_type='BC_AUTH_TT' AND identity_authorized_bc_id IS NOT NULL)))",
    )
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis','manual_link','identity') AND applied_revision > 0",
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM draft_mutation_request WHERE kind='identity') THEN
                RAISE EXCEPTION 'cannot downgrade retained identity mutations';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis','manual_link') AND applied_revision > 0",
    )
    op.drop_constraint("ck_build_draft_identity", "build_draft", type_="check")
    for name in (
        "identity_username",
        "identity_display_name",
        "identity_authorized_bc_id",
        "identity_type",
        "identity_id",
    ):
        op.drop_column("build_draft", name)
