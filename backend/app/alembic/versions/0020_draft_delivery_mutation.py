"""Allow the step-two delivery schedule save in draft mutation receipts."""

from alembic import op

revision = "0020_draft_delivery_mutation"
down_revision = "0019_scene_budget_page"
branch_labels = None
depends_on = None

OLD = "kind IN ('update','groups','minis','manual_link','identity','targeting') AND applied_revision > 0"
NEW = "kind IN ('update','groups','minis','manual_link','identity','targeting','delivery') AND applied_revision > 0"


def upgrade():
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", NEW
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM draft_mutation_request WHERE kind='delivery') THEN
            RAISE EXCEPTION 'cannot downgrade retained delivery schedule receipts';
        END IF;
    END $$;""")
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", OLD
    )
