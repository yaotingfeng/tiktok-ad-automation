"""明确失败的新准备使用独立代数，旧转存及等待者身份保持不变。"""

import sqlalchemy as sa
from alembic import op

revision = "material_seed_generations"
down_revision = "material_target_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_bc_seed",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("material_bc_seed", "generation", server_default=None)
    op.drop_constraint(
        "uq_material_bc_seed_content", "material_bc_seed", type_="unique"
    )
    op.create_unique_constraint(
        "uq_material_bc_seed_content",
        "material_bc_seed",
        ["tenant_id", "bc_id", "content_key", "generation"],
    )
    op.create_check_constraint(
        "ck_material_bc_seed_generation",
        "material_bc_seed",
        "generation >= 1",
    )


def downgrade() -> None:
    # 新代即使是失败也属于真实历史，不能丢弃或折叠成旧代来满足旧唯一键。
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM material_bc_seed WHERE generation <> 1)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Cannot discard material BC seed generation history")
    op.drop_constraint(
        "ck_material_bc_seed_generation", "material_bc_seed", type_="check"
    )
    op.drop_constraint(
        "uq_material_bc_seed_content", "material_bc_seed", type_="unique"
    )
    op.create_unique_constraint(
        "uq_material_bc_seed_content",
        "material_bc_seed",
        ["tenant_id", "bc_id", "content_key"],
    )
    op.drop_column("material_bc_seed", "generation")
