"""Allow persisted scene jobs to retain typed Smart+ ad-group budget evidence."""
from alembic import op

revision = "0018_scene_budget_evidence"
down_revision = ("0017_preview_naming", "20261005_merge_delivery_heads")
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_build_scene_job_resource", "build_scene_job", type_="check")
    op.create_check_constraint(
        "ck_build_scene_job_resource",
        "build_scene_job",
        "resource IN ('capabilities','identity','minis','cta','vbo','regions','budget','done')",
    )
    op.drop_constraint("ck_scene_resource", "scene_read_state", type_="check")
    op.create_check_constraint(
        "ck_scene_resource",
        "scene_read_state",
        "resource IN ('account_roles','identity','minis','cta','vbo','regions','budget')",
    )


def downgrade():
    op.drop_constraint("ck_scene_resource", "scene_read_state", type_="check")
    op.create_check_constraint(
        "ck_scene_resource", "scene_read_state", "resource IN ('account_roles','identity','minis','cta','vbo')"
    )
    op.drop_constraint("ck_build_scene_job_resource", "build_scene_job", type_="check")
    op.create_check_constraint(
        "ck_build_scene_job_resource", "build_scene_job", "resource IN ('capabilities','identity','minis','cta','vbo','regions','done')"
    )
