"""素材来源与目标分离后，操作和分发独立约束租户目标 BC。"""

from alembic import op

revision = "material_target_scope"
down_revision = "material_bc_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("material_asset_operation", "material_distribution"):
        op.create_foreign_key(
            f"fk_{table}_tenant_bc",
            table,
            "tenant_bc",
            ["tenant_id", "bc_id"],
            ["tenant_id", "bc_id"],
        )


def downgrade() -> None:
    for table in ("material_distribution", "material_asset_operation"):
        op.drop_constraint(f"fk_{table}_tenant_bc", table, type_="foreignkey")
