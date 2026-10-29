"""Typed drafts and generation-scoped creative units; video cutover is deferred to P3."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0049_image_composition"
down_revision = "0048_image_reissue_routes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "build_draft",
        sa.Column("media_kind", sa.String(16), nullable=False, server_default="VIDEO"),
    )
    op.add_column(
        "build_draft",
        sa.Column(
            "active_image_generation", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "build_draft",
        sa.Column("applied_image_rule_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "build_draft",
        sa.Column(
            "structure_override", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    op.create_unique_constraint(
        "uq_build_draft_media_kind",
        "build_draft",
        ["tenant_id", "id", "bc_id", "media_kind"],
    )
    op.create_check_constraint(
        "ck_build_draft_media_kind", "build_draft", "media_kind IN ('VIDEO','CAROUSEL')"
    )
    op.create_check_constraint(
        "ck_build_draft_image_versions",
        "build_draft",
        "active_image_generation >= 0 AND (applied_image_rule_revision IS NULL OR applied_image_rule_revision > 0)",
    )
    op.create_check_constraint(
        "ck_build_draft_structure_object",
        "build_draft",
        "structure_override IS NULL OR jsonb_typeof(structure_override) = 'object'",
    )
    op.create_table(
        "draft_creative_unit",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="VIDEO"),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("composition_protected", sa.Boolean(), nullable=False),
        sa.Column("music_protected", sa.Boolean(), nullable=False),
        sa.Column("music_id", sa.String(255), nullable=True),
        sa.Column("music_rule_digest", sa.String(64), nullable=False),
        sa.Column("music_status", sa.String(32), nullable=False),
        sa.Column("music_revision", sa.Integer(), nullable=False),
        sa.Column("music_request_generation", sa.Integer(), nullable=False),
        sa.Column("music_snapshot", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "draft_id", "generation", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "drama_id"],
            [
                "draft_drama.tenant_id",
                "draft_drama.draft_id",
                "draft_drama.bc_id",
                "draft_drama.drama_id",
            ],
            name="fk_creative_unit_drama",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id", "bc_id", "kind"],
            [
                "build_draft.tenant_id",
                "build_draft.id",
                "build_draft.bc_id",
                "build_draft.media_kind",
            ],
            name="fk_creative_unit_kind",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "bc_id",
            "drama_id",
            "generation",
            "id",
            "kind",
            name="uq_creative_unit_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "drama_id",
            "generation",
            "position",
            name="uq_creative_unit_position",
        ),
        sa.CheckConstraint(
            "kind IN ('VIDEO','CAROUSEL')", name="ck_creative_unit_kind"
        ),
        sa.CheckConstraint(
            "generation > 0 AND position > 0 AND revision > 0 AND music_revision >= 0 AND music_request_generation >= 0",
            name="ck_creative_unit_versions",
        ),
        sa.CheckConstraint(
            "music_status IN ('NOT_REQUIRED','NEEDS_SELECTION','PENDING','RUNNING','READY','FAILED')",
            name="ck_creative_unit_music_status",
        ),
        sa.CheckConstraint(
            "(kind = 'VIDEO' AND music_status = 'NOT_REQUIRED' AND music_id IS NULL) OR (kind = 'CAROUSEL' AND music_status != 'NOT_REQUIRED')",
            name="ck_creative_unit_music_kind",
        ),
        sa.CheckConstraint(
            "music_status != 'READY' OR (music_id IS NOT NULL AND length(music_id) > 0)",
            name="ck_creative_unit_ready_music",
        ),
    )
    op.create_index(
        "ix_creative_unit_music_progress",
        "draft_creative_unit",
        ["tenant_id", "draft_id", "generation", "music_status"],
    )
    op.create_table(
        "draft_creative_unit_item",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="VIDEO"),
        sa.Column("media_type", sa.String(16), nullable=False, server_default="VIDEO"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "draft_id", "generation", "unit_id", "position"
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "draft_id",
                "bc_id",
                "drama_id",
                "generation",
                "unit_id",
                "kind",
            ],
            [
                "draft_creative_unit.tenant_id",
                "draft_creative_unit.draft_id",
                "draft_creative_unit.bc_id",
                "draft_creative_unit.drama_id",
                "draft_creative_unit.generation",
                "draft_creative_unit.id",
                "draft_creative_unit.kind",
            ],
            name="fk_creative_item_unit",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id", "media_type"],
            ["material_file.tenant_id", "material_file.id", "material_file.media_type"],
            name="fk_creative_item_material",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "generation",
            "unit_id",
            "material_id",
            name="uq_creative_item_material",
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 35 AND (kind != 'VIDEO' OR position = 1)",
            name="ck_creative_item_position",
        ),
        sa.CheckConstraint(
            "(kind = 'VIDEO' AND media_type = 'VIDEO') OR (kind = 'CAROUSEL' AND media_type = 'IMAGE')",
            name="ck_creative_item_media_type",
        ),
    )
    op.create_index(
        "ix_creative_item_material",
        "draft_creative_unit_item",
        ["tenant_id", "material_id"],
    )
    # 不回填旧视频：当前视频生产者和消费者仍读旧表，P3 一次切换。


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM draft_creative_unit) OR EXISTS(SELECT 1 FROM build_draft WHERE media_kind != 'VIDEO' OR active_image_generation != 0 OR applied_image_rule_revision IS NOT NULL OR structure_override IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Creative data exists; downgrade would erase draft content")
    op.drop_table("draft_creative_unit_item")
    op.drop_table("draft_creative_unit")
    op.drop_constraint("ck_build_draft_structure_object", "build_draft", type_="check")
    op.drop_constraint("ck_build_draft_image_versions", "build_draft", type_="check")
    op.drop_constraint("ck_build_draft_media_kind", "build_draft", type_="check")
    op.drop_constraint("uq_build_draft_media_kind", "build_draft", type_="unique")
    for column in (
        "structure_override",
        "applied_image_rule_revision",
        "active_image_generation",
        "media_kind",
    ):
        op.drop_column("build_draft", column)
