"""Cut current video drafts to ordered creative units and isolate scene formats."""

import sqlalchemy as sa
from alembic import op

revision = "0059_video_creative_units"
down_revision = "0058_selected_music_retry"
branch_labels = None
depends_on = None


def upgrade():
    # 一次生成独立实体 UUID；原草稿 request_id 和所有历史冻结正文/哈希均不修改。
    op.execute("""
        CREATE TEMP TABLE video_unit_backfill ON COMMIT DROP AS
        SELECT old.*, file.sha256 AS recorded_sha256, gen_random_uuid() AS unit_id,
            row_number() OVER (PARTITION BY old.tenant_id, old.draft_id, old.drama_id
                              ORDER BY old.group_no, old.position) AS unit_position
        FROM draft_group_material old
        JOIN build_draft draft ON draft.tenant_id=old.tenant_id AND draft.id=old.draft_id
        JOIN material_file file ON file.tenant_id=old.tenant_id AND file.id=old.material_id
        WHERE draft.media_kind='VIDEO'
    """)
    op.execute("""
        INSERT INTO draft_creative_unit
        (tenant_id,draft_id,generation,id,bc_id,drama_id,kind,position,content_digest,
         revision,composition_protected,music_protected,music_id,music_rule_digest,
         music_status,music_revision,music_request_generation,music_snapshot)
        SELECT tenant_id,draft_id,1,unit_id,bc_id,drama_id,'VIDEO',unit_position,
         encode(sha256(convert_to(json_build_array(material_id::text, recorded_sha256)::text, 'UTF8')), 'hex'),
         1,false,false,NULL,'','NOT_REQUIRED',0,0,'{}'::jsonb FROM video_unit_backfill
    """)
    op.execute("""
        INSERT INTO draft_creative_unit_item
        (tenant_id,draft_id,generation,unit_id,position,bc_id,drama_id,material_id,kind,media_type)
        SELECT tenant_id,draft_id,1,unit_id,1,bc_id,drama_id,material_id,'VIDEO','VIDEO'
        FROM video_unit_backfill
    """)
    # 旧表保留 schema 供历史引用审计，但已迁移现行草稿不双写、不保留失真副本。
    op.execute(
        "DELETE FROM draft_group_material WHERE (tenant_id,draft_id,drama_id,group_no,position) IN (SELECT tenant_id,draft_id,drama_id,group_no,position FROM video_unit_backfill)"
    )
    op.add_column(
        "build_scene_job",
        sa.Column("media_kind", sa.String(16), nullable=False, server_default="VIDEO"),
    )
    op.add_column(
        "build_scene_job",
        sa.Column(
            "compiler_contract_revision",
            sa.String(64),
            nullable=False,
            server_default="legacy-video-v1",
        ),
    )
    op.alter_column("build_scene_job", "media_kind", server_default=None)
    op.alter_column(
        "build_scene_job", "compiler_contract_revision", server_default=None
    )
    op.create_check_constraint(
        "ck_scene_media_kind", "build_scene_job", "media_kind IN ('VIDEO','CAROUSEL')"
    )


def downgrade():
    raise RuntimeError("创意身份切换不可逆；禁止重建旧草稿映射或改写历史冻结正文")
