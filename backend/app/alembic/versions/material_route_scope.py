"""按各自冻结路由校验素材来源与目标连接的 BC 绑定。

Revision ID: material_route_scope
Revises: preview_skipped_materials
"""

from alembic import op

revision = "material_route_scope"
down_revision = "preview_skipped_materials"
branch_labels = None
depends_on = None


def _replace(*, cross_bc: bool) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('mcp_material_route_guard()'::regprocedure)"
    ).scalar_one()
    old = "AND b.bc_id=NEW.bc_id"
    new = "AND b.bc_id=route_value->>'bc_id'"
    if not cross_bc:
        old, new = new, old
    if definition.count(old) != 1:
        raise RuntimeError("Unexpected material route guard; migration aborted")
    # 各字段的 CHECK 已将路由绑定到 tenant_id 和对应的 bc_id/source_bc_id。
    # 这里再核验该路由自身的连接归属；来源授权无须同时绑定目标 BC。
    # 保留全部旧路由、不可变触发器及租户/通道校验，不改写历史业务行。
    op.execute(definition.replace(old, new))


def upgrade() -> None:
    _replace(cross_bc=True)


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("LOCK TABLE material_distribution IN ACCESS EXCLUSIVE MODE")
    if connection.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM material_distribution "
        "WHERE source_route IS NOT NULL AND source_bc_id IS DISTINCT FROM bc_id)"
    ).scalar_one():
        raise RuntimeError("Cannot restore target-only guard with cross-BC evidence")
    _replace(cross_bc=False)
