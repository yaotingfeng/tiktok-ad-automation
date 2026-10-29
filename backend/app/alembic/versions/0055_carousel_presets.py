"""Immutable reusable carousel versions and typed retention references.

Revision ID: 0055_carousel_presets
Revises: 0054_music_preparation
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_carousel_presets"
down_revision = "0054_music_preparation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "carousel_preset_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_bc_id", sa.String(128), nullable=False),
        sa.Column("source_draft_id", sa.Uuid(), nullable=False),
        sa.Column("source_unit_id", sa.Uuid(), nullable=False),
        sa.Column("source_drama_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("ordered_material_ids", postgresql.JSONB(), nullable=False),
        sa.Column("music_id", sa.String(255)),
        sa.Column("music_status", sa.String(32), nullable=False),
        sa.Column("music_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_carousel_preset_scope"),
        sa.UniqueConstraint(
            "tenant_id", "name", "version", name="uq_carousel_preset_version"
        ),
        sa.CheckConstraint(
            "version > 0 AND length(trim(name)) BETWEEN 1 AND 200",
            name="ck_carousel_preset_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(ordered_material_ids) = 'array' AND jsonb_array_length(ordered_material_ids) BETWEEN 1 AND 35",
            name="ck_carousel_preset_images",
        ),
        sa.CheckConstraint(
            "music_status IN ('READY','PENDING','NEEDS_SELECTION') AND ((music_status = 'READY' AND music_id IS NOT NULL AND length(music_id) > 0) OR (music_status != 'READY' AND music_id IS NULL))",
            name="ck_carousel_preset_music",
        ),
    )
    op.create_index(
        "ix_carousel_preset_name",
        "carousel_preset_version",
        ["tenant_id", "name", "version"],
    )
    op.create_table(
        "carousel_preset_item",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["carousel_preset_version.tenant_id", "carousel_preset_version.id"],
            name="fk_carousel_preset_item_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id", "media_type"],
            ["material_file.tenant_id", "material_file.id", "material_file.media_type"],
            name="fk_carousel_preset_item_material",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "version_id",
            "material_id",
            name="uq_carousel_preset_item_material",
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 35 AND media_type = 'IMAGE'",
            name="ck_carousel_preset_item_type",
        ),
    )
    op.create_index(
        "ix_carousel_preset_material",
        "carousel_preset_item",
        ["tenant_id", "material_id"],
    )
    op.execute("""CREATE FUNCTION reject_carousel_preset_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'carousel preset versions are immutable' USING ERRCODE = '23514'; END; $$""")
    for table in ("carousel_preset_version", "carousel_preset_item"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_carousel_preset_mutation()"
        )
    # 成员只能插入版本已声明的位置；即便绕过服务也不能向旧版本追加图片。
    op.execute("""CREATE FUNCTION guard_carousel_preset_member() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE expected text;
    BEGIN
      SELECT ordered_material_ids ->> (NEW.position - 1) INTO expected FROM carousel_preset_version WHERE tenant_id = NEW.tenant_id AND id = NEW.version_id;
      IF expected IS DISTINCT FROM NEW.material_id::text THEN
        RAISE EXCEPTION 'preset membership differs from immutable order' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER carousel_preset_member_order BEFORE INSERT ON carousel_preset_item FOR EACH ROW EXECUTE FUNCTION guard_carousel_preset_member()"
    )
    # 首次写入必须在同一事务完整建立所有 FK，防止只有 JSON 而缺少删除保护。
    op.execute("""CREATE FUNCTION check_carousel_preset_members() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT count(*) FROM carousel_preset_item WHERE tenant_id = NEW.tenant_id AND version_id = NEW.id) != jsonb_array_length(NEW.ordered_material_ids) THEN
        RAISE EXCEPTION 'incomplete preset references' USING ERRCODE = '23514';
      END IF;
      RETURN NULL;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER carousel_preset_complete AFTER INSERT ON carousel_preset_version DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_carousel_preset_members()"
    )


def downgrade():
    op.drop_table("carousel_preset_item")
    op.drop_table("carousel_preset_version")
    op.execute("DROP FUNCTION check_carousel_preset_members()")
    op.execute("DROP FUNCTION guard_carousel_preset_member()")
    op.execute("DROP FUNCTION reject_carousel_preset_mutation()")
