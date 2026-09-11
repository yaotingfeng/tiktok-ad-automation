"""scope staged directory pages by BC

Revision ID: mcp_directory_bc_scope
Revises: mcp_capability_routes
Create Date: 2026-09-12 00:22:22.129617

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "mcp_directory_bc_scope"
down_revision = "mcp_capability_routes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "discovery_staged_page", sa.Column("bc_id", sa.String(128), nullable=True)
    )
    # 旧 MCP 资产页只能回填该任务明确选择的 BC，不能从当前默认推断。
    op.execute(
        sa.text("""
        UPDATE discovery_staged_page p
        SET bc_id = CASE WHEN p.stage IN ('SUBJECT','AUTHORIZED','BCS') THEN ''
                        ELSE r.work->>'bc_id' END
        FROM discovery_run r
        WHERE r.id=p.run_id AND r.tenant_id=p.tenant_id AND r.connection_id=p.connection_id
    """)
    )
    if (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS (SELECT 1 FROM discovery_staged_page
        WHERE bc_id IS NULL OR (stage IN ('ASSETS','DETAILS','ROLES') AND btrim(bc_id)=''))
    """)
        )
        .scalar_one()
    ):
        raise RuntimeError("cannot infer historical staged BC scope")
    op.alter_column("discovery_staged_page", "bc_id", nullable=False)
    op.drop_constraint(
        "discovery_staged_page_pkey", "discovery_staged_page", type_="primary"
    )
    op.create_primary_key(
        "discovery_staged_page_pkey",
        "discovery_staged_page",
        ["run_id", "bc_id", "stage", "page"],
    )


def downgrade():
    # 非空暂存页带有 BC 分页证据，不能丢弃 scope 或合并不同 BC 的同页。
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM discovery_staged_page)"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard staged directory BC scope")
    op.drop_constraint(
        "discovery_staged_page_pkey", "discovery_staged_page", type_="primary"
    )
    op.create_primary_key(
        "discovery_staged_page_pkey",
        "discovery_staged_page",
        ["run_id", "stage", "page"],
    )
    op.drop_column("discovery_staged_page", "bc_id")
