"""Persist per-base-ad material mappings for general strategy previews."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_preview_ad_material_strategy"
down_revision = "20261004_general_ad_build_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "preview_copy", sa.Column("base_ad_no", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "planned_ad", sa.Column("base_ad_no", sa.Integer(), nullable=False, server_default="1")
    )
    # 旧预览的所有创意都属于基础广告 1；回填完成后移除默认值，避免未来
    # 漏写基础广告序号时静默生成错误的冻结映射。
    op.execute("UPDATE preview_copy SET base_ad_no = 1 WHERE base_ad_no IS NULL")
    op.execute("UPDATE planned_ad SET base_ad_no = 1 WHERE base_ad_no IS NULL")
    op.alter_column("preview_copy", "base_ad_no", server_default=None)
    op.alter_column("planned_ad", "base_ad_no", server_default=None)
    # 创意序号只在基础广告内唯一；基础广告序号必须进入原复合主键。
    op.drop_constraint("preview_copy_pkey", "preview_copy", type_="primary")
    op.create_primary_key(
        "preview_copy_pkey",
        "preview_copy",
        ["tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "creative_no"],
    )
    op.drop_constraint("uq_preview_drama_material", "preview_group_material", type_="unique")
    op.create_unique_constraint(
        "uq_preview_drama_material",
        "preview_group_material",
        ["tenant_id", "preview_id", "drama_id", "group_no", "material_id"],
    )
    op.drop_constraint("uq_planned_ad_no", "planned_ad", type_="unique")
    op.create_unique_constraint(
        "uq_planned_ad_no", "planned_ad", ["tenant_id", "group_id", "base_ad_no", "creative_no"]
    )
    op.drop_constraint("ck_preview_copy_no", "preview_copy", type_="check")
    op.create_check_constraint("ck_preview_copy_no", "preview_copy", "base_ad_no > 0 AND creative_no > 0")
    op.drop_constraint("ck_planned_ad_no", "planned_ad", type_="check")
    op.create_check_constraint("ck_planned_ad_no", "planned_ad", "base_ad_no > 0 AND creative_no > 0")
    op.create_table(
        "preview_ad_material",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("group_no", sa.Integer(), nullable=False),
        sa.Column("base_ad_no", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "position"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "drama_id", "group_no"],
            ["preview_drama_group.tenant_id", "preview_drama_group.preview_id", "preview_drama_group.drama_id", "preview_drama_group.group_no"],
        ),
        sa.ForeignKeyConstraint(["tenant_id", "material_id"], ["material_file.tenant_id", "material_file.id"]),
        sa.CheckConstraint("base_ad_no > 0 AND position > 0", name="ck_preview_ad_material_position"),
        sa.UniqueConstraint(
            "tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "position",
            name="uq_preview_ad_material_position",
        ),
        sa.UniqueConstraint(
            "tenant_id", "preview_id", "drama_id", "group_no", "base_ad_no", "material_id",
            name="uq_preview_ad_material_id",
        ),
    )
    op.execute(
        "CREATE TRIGGER preview_ad_material_frozen BEFORE INSERT OR UPDATE OR DELETE "
        "ON preview_ad_material FOR EACH ROW EXECUTE FUNCTION check_preview_child_write()"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM preview_group_material "
            "GROUP BY tenant_id, preview_id, drama_id, material_id "
            "HAVING count(DISTINCT group_no) > 1)"
        )
    ).scalar():
        raise RuntimeError(
            "Cannot downgrade: preview_group_material now contains shared materials across groups"
        )
    op.execute("DROP TRIGGER preview_ad_material_frozen ON preview_ad_material")
    op.drop_table("preview_ad_material")
    op.drop_constraint("ck_planned_ad_no", "planned_ad", type_="check")
    op.create_check_constraint("ck_planned_ad_no", "planned_ad", "creative_no > 0")
    op.drop_constraint("ck_preview_copy_no", "preview_copy", type_="check")
    op.create_check_constraint("ck_preview_copy_no", "preview_copy", "creative_no > 0")
    op.drop_constraint("uq_planned_ad_no", "planned_ad", type_="unique")
    op.create_unique_constraint("uq_planned_ad_no", "planned_ad", ["tenant_id", "group_id", "creative_no"])
    op.drop_constraint("uq_preview_drama_material", "preview_group_material", type_="unique")
    op.create_unique_constraint(
        "uq_preview_drama_material", "preview_group_material", ["tenant_id", "preview_id", "drama_id", "material_id"]
    )
    op.drop_column("planned_ad", "base_ad_no")
    op.drop_constraint("preview_copy_pkey", "preview_copy", type_="primary")
    op.create_primary_key(
        "preview_copy_pkey",
        "preview_copy",
        ["tenant_id", "preview_id", "drama_id", "group_no", "creative_no"],
    )
    op.drop_column("preview_copy", "base_ad_no")
