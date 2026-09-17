"""冻结各账户跳过的不可用素材，保留既有预览和执行内容不变。"""

import sqlalchemy as sa
from alembic import op

revision = "preview_skipped_materials"
down_revision = "material_seed_generations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preview_skipped_material",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("file_name", sa.String(1000), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "unit_id", "material_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preview_id", "bc_id"],
            ["build_preview.tenant_id", "build_preview.id", "build_preview.bc_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "unit_id", "preview_id", "bc_id"],
            [
                "build_unit.tenant_id",
                "build_unit.id",
                "build_unit.preview_id",
                "build_unit.bc_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
    )
    op.execute(
        "CREATE TRIGGER preview_skipped_material_frozen BEFORE INSERT OR UPDATE OR DELETE ON preview_skipped_material FOR EACH ROW EXECUTE FUNCTION check_preview_child_write()"
    )


def downgrade() -> None:
    # 删除排除证据会让旧素材重新进入已冻结请求，已有记录时禁止降级。
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM preview_skipped_material)"))
        .scalar()
    ):
        raise RuntimeError("Cannot discard frozen material exclusions")
    op.drop_table("preview_skipped_material")
