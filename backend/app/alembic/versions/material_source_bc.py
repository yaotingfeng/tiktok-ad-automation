"""Freeze the actual source BC independently from the target BC.

Revision ID: material_source_bc
Revises: mat_response_archive
"""

from alembic import op

revision = "material_source_bc"
down_revision = "mat_response_archive"
branch_labels = None
depends_on = None


OLD_FK = "material_distribution_tenant_id_bc_id_material_id_source_a_fkey"
NEW_FK = "fk_material_distribution_source"


def _replace(*, cross_bc: bool) -> None:
    connection = op.get_bind()
    expression = connection.exec_driver_sql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_material_distribution_source_route' AND conrelid = 'material_distribution'::regclass"
    ).scalar_one()
    old = "(bc_id)::text" if cross_bc else "(source_bc_id)::text"
    new = "(source_bc_id)::text" if cross_bc else "(bc_id)::text"
    clause = "((source_route ->> 'bc_id'::text) = " + old + ")"
    if expression.count(clause) != 1:
        raise RuntimeError("Unexpected source route constraint; migration aborted")
    expression = expression.replace(
        clause, "((source_route ->> 'bc_id'::text) = " + new + ")"
    )
    op.drop_constraint(
        "ck_material_distribution_source_route", "material_distribution", type_="check"
    )
    op.create_check_constraint(
        "ck_material_distribution_source_route",
        "material_distribution",
        expression[len("CHECK (") : -1],
    )


def upgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "material_distribution",
        sa.Column("source_bc_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "material_distribution",
        sa.Column("source_material_id", sa.Uuid(), nullable=True),
    )
    # 历史来源严格属于目标 BC/素材；只补明确关系，不改远端请求或历史发送结果。
    op.execute(
        "UPDATE material_distribution SET source_bc_id=bc_id, source_material_id=material_id WHERE source_asset_id IS NOT NULL OR source_route IS NOT NULL"
    )
    op.execute(
        "UPDATE material_asset_operation SET remote_response=remote_response || jsonb_build_object('source_bc_id',bc_id,'source_material_id',material_id::text) WHERE remote_response ? 'source_asset_id' AND NOT remote_response ? 'source_bc_id'"
    )
    op.drop_constraint(OLD_FK, "material_distribution", type_="foreignkey")
    op.create_foreign_key(
        NEW_FK,
        "material_distribution",
        "account_material",
        ["tenant_id", "source_bc_id", "source_material_id", "source_asset_id"],
        ["tenant_id", "bc_id", "material_id", "id"],
    )
    _replace(cross_bc=True)


def downgrade() -> None:
    # 跨 BC 记录存在时旧外键会拒绝回退，不能静默丢弃来源范围。
    op.drop_constraint(NEW_FK, "material_distribution", type_="foreignkey")
    op.create_foreign_key(
        OLD_FK,
        "material_distribution",
        "account_material",
        ["tenant_id", "bc_id", "material_id", "source_asset_id"],
        ["tenant_id", "bc_id", "material_id", "id"],
    )
    _replace(cross_bc=False)
    op.drop_column("material_distribution", "source_material_id")
    op.drop_column("material_distribution", "source_bc_id")
