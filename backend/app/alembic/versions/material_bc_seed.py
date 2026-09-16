"""每个租户内容进入目标 BC 只有一个持久转存依赖。"""

import sqlalchemy as sa
from alembic import op

revision = "material_bc_seed"
down_revision = "tenant_material_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_material_distribution_identity",
        "material_distribution",
        ["tenant_id", "bc_id", "material_id", "advertiser_id", "id"],
    )
    op.create_table(
        "material_bc_seed",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("distribution_id", sa.Uuid(), nullable=False),
        sa.Column("content_key", sa.String(256), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.UniqueConstraint(
            "tenant_id", "bc_id", "content_key", name="uq_material_bc_seed_content"
        ),
        sa.UniqueConstraint(
            "tenant_id", "bc_id", "id", name="uq_material_bc_seed_identity"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "distribution_id"],
            [
                "material_distribution.tenant_id",
                "material_distribution.bc_id",
                "material_distribution.material_id",
                "material_distribution.advertiser_id",
                "material_distribution.id",
            ],
            name="fk_material_bc_seed_distribution",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.add_column(
        "material_distribution", sa.Column("seed_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_material_distribution_seed",
        "material_distribution",
        "material_bc_seed",
        ["tenant_id", "bc_id", "seed_id"],
        ["tenant_id", "bc_id", "id"],
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM material_bc_seed)"))
        .scalar()
    ):
        raise RuntimeError("Cannot drop actual material BC seed history")
    op.drop_constraint(
        "fk_material_distribution_seed", "material_distribution", type_="foreignkey"
    )
    op.drop_column("material_distribution", "seed_id")
    op.drop_table("material_bc_seed")
    op.drop_constraint(
        "uq_material_distribution_identity", "material_distribution", type_="unique"
    )
