"""租户素材消费与原始上传来源分离，不改写任何历史事实。"""

import sqlalchemy as sa
from alembic import op

revision = "tenant_material_library"
down_revision = "material_share_receipts"
branch_labels = None
depends_on = None

CONSUMERS = (
    "account_material",
    "material_asset_operation",
    "material_distribution",
    "material_cover_job",
    "draft_group_material",
    "preview_group_material",
    "execution_step",
)


def _material_keys(*, tenant_only: bool) -> None:
    inspector = sa.inspect(op.get_bind())
    for table in CONSUMERS:
        keys = [
            fk
            for fk in inspector.get_foreign_keys(table)
            if fk["referred_table"] == "material_file"
        ]
        if len(keys) != 1:
            raise RuntimeError(f"Unexpected material reference in {table}")
        op.drop_constraint(keys[0]["name"], table, type_="foreignkey")
        columns = (
            ["tenant_id", "material_id"]
            if tenant_only
            else ["tenant_id", "bc_id", "material_id"]
        )
        referred = ["tenant_id", "id"] if tenant_only else ["tenant_id", "bc_id", "id"]
        op.create_foreign_key(
            keys[0]["name"], table, "material_file", columns, referred
        )


def _target_uniqueness(*, with_bc: bool) -> None:
    columns = (
        ["tenant_id", "bc_id", "material_id", "advertiser_id"]
        if with_bc
        else ["tenant_id", "material_id", "advertiser_id"]
    )
    op.drop_constraint("uq_account_material_target", "account_material", type_="unique")
    op.create_unique_constraint(
        "uq_account_material_target", "account_material", columns
    )
    for table, name, states in (
        (
            "material_asset_operation",
            "uq_material_unverified_operation",
            "'pending','sending','result_unknown','verifying','confirmed_absent'",
        ),
        (
            "material_distribution",
            "uq_material_pending_distribution",
            "'queued','preparing','verifying','result_unknown'",
        ),
    ):
        op.drop_index(name, table_name=table)
        op.create_index(
            name,
            table,
            columns,
            unique=True,
            postgresql_where=sa.text(f"status IN ({states})"),
        )


def upgrade() -> None:
    _material_keys(tenant_only=True)
    _target_uniqueness(with_bc=True)
    op.create_index(
        "ix_material_tenant_name_id", "material_file", ["tenant_id", "file_name", "id"]
    )


def downgrade() -> None:
    # 回退旧约束前拒绝真实跨 BC 消费；不删除新记录来伪装可回退。
    connection = op.get_bind()
    for table in CONSUMERS:
        if connection.execute(
            sa.text(
                f"SELECT EXISTS(SELECT 1 FROM {table} c JOIN material_file m "
                "ON m.tenant_id=c.tenant_id AND m.id=c.material_id WHERE c.bc_id<>m.bc_id)"
            )
        ).scalar():
            raise RuntimeError(
                "Cannot downgrade tenant material library with cross-BC references"
            )
    _target_uniqueness(with_bc=False)
    _material_keys(tenant_only=False)
    op.drop_index("ix_material_tenant_name_id", table_name="material_file")
