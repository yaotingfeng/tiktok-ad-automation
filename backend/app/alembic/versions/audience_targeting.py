"""Add batch targeting overrides without changing frozen advertising requests."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "audience_targeting"
down_revision = "draft_identity_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "build_draft",
        sa.Column(
            "targeting_override", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_build_draft_targeting_object",
        "build_draft",
        "targeting_override IS NULL OR jsonb_typeof(targeting_override) = 'object'",
    )
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis','manual_link','identity','targeting') AND applied_revision > 0",
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM build_draft WHERE targeting_override IS NOT NULL)
           OR EXISTS(SELECT 1 FROM draft_mutation_request WHERE kind='targeting') THEN
            RAISE EXCEPTION 'cannot downgrade retained targeting choices';
        END IF;
    END $$;""")
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis','manual_link','identity') AND applied_revision > 0",
    )
    op.drop_constraint("ck_build_draft_targeting_object", "build_draft", type_="check")
    op.drop_column("build_draft", "targeting_override")
