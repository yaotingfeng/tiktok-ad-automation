"""区分小程序选择后的账户场景校验，不把它显示成素材匹配。"""

from alembic import op

revision = "draft_scene_phase"
down_revision = "outbox_payload_compaction"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_draft_preparation_phase", "draft_preparation", type_="check")
    op.create_check_constraint(
        "ck_draft_preparation_phase",
        "draft_preparation",
        "phase IN ('accounts','links','materials','scenes','done')",
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM draft_preparation WHERE phase = 'scenes') THEN
                RAISE EXCEPTION 'cannot downgrade retained scene preparation phases';
            END IF;
        END $$;
    """)
    op.drop_constraint("ck_draft_preparation_phase", "draft_preparation", type_="check")
    op.create_check_constraint(
        "ck_draft_preparation_phase",
        "draft_preparation",
        "phase IN ('accounts','links','materials','done')",
    )
