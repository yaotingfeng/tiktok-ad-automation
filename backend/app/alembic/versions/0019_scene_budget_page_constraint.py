"""Allow persisted budget pages in the scene evidence table."""
from alembic import op

revision = "0019_scene_budget_page"
down_revision = "0018_scene_budget_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_build_scene_job_page_resource", "build_scene_job_page", type_="check"
    )
    op.create_check_constraint(
        "ck_build_scene_job_page_resource",
        "build_scene_job_page",
        "resource IN ('identity','minis','cta','vbo','regions','budget')",
    )


def downgrade():
    op.drop_constraint(
        "ck_build_scene_job_page_resource", "build_scene_job_page", type_="check"
    )
    op.create_check_constraint(
        "ck_build_scene_job_page_resource",
        "build_scene_job_page",
        "resource IN ('identity','minis','cta','vbo','regions')",
    )
