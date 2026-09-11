"""Allow an explicit BC-scoped connection preference on an editable draft.

Revision ID: mcp_draft_connection
Revises: mcp_build_recovery
"""

from alembic import op
import sqlalchemy as sa

revision = "mcp_draft_connection"
down_revision = "mcp_build_recovery"
branch_labels = None
depends_on = None


def upgrade():
    # NULL 表示新准备时使用 BC 默认；不猜测或回填历史草稿的连接。
    op.add_column(
        "build_draft", sa.Column("execution_connection_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_build_draft_execution_connection",
        "build_draft",
        "bc_connection_binding",
        ["tenant_id", "bc_id", "execution_connection_id"],
        ["tenant_id", "bc_id", "connection_id"],
    )


def downgrade():
    # 先锁定再检查，避免检查之后的新选择被静默删除。
    op.execute("LOCK TABLE build_draft IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM build_draft WHERE execution_connection_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot drop explicit draft connection choices';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "fk_build_draft_execution_connection", "build_draft", type_="foreignkey"
    )
    op.drop_column("build_draft", "execution_connection_id")
