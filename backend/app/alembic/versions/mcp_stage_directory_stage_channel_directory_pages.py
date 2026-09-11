"""stage channel directory pages

Revision ID: mcp_stage_directory
Revises: mcp01
Create Date: 2026-09-12 00:02:45.935697

"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "mcp_stage_directory"
down_revision = "mcp01"
branch_labels = None
depends_on = None


def upgrade():
    # 先建立完整连接范围唯一键，再建立引用它的暂存页；不改写现有目录。
    op.create_unique_constraint(
        "uq_discovery_run_connection",
        "discovery_run",
        ["tenant_id", "connection_id", "id"],
    )
    op.create_table(
        "discovery_staged_page",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("total_pages", sa.Integer(), nullable=False),
        sa.Column("total_number", sa.Integer(), nullable=True),
        sa.Column("last_page", sa.Boolean(), nullable=False),
        sa.Column(
            "pagination_kind",
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
        ),
        sa.Column("rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "call_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "schema_digest", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(rows) = 'array' AND jsonb_array_length(rows) <= 50 AND jsonb_typeof(call_evidence) = 'object'",
            name="ck_discovery_staged_page_payload",
        ),
        sa.CheckConstraint(
            "pagination_kind IN ('REMOTE','FULL_RESPONSE','EXPLICIT_IDS')",
            name="ck_discovery_staged_page_pagination_kind",
        ),
        sa.CheckConstraint(
            "schema_digest ~ '^[0-9a-f]{64}$'",
            name="ck_discovery_staged_page_schema_digest",
        ),
        sa.CheckConstraint(
            "stage IN ('SUBJECT','AUTHORIZED','BCS','ASSETS','DETAILS','ROLES')",
            name="ck_discovery_staged_page_stage",
        ),
        sa.CheckConstraint(
            "page >= 1 AND total_pages >= 1 AND page <= total_pages AND (total_number IS NULL OR total_number >= 0) AND last_page = (page = total_pages)",
            name="ck_discovery_staged_page_numbers",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "run_id"],
            [
                "discovery_run.tenant_id",
                "discovery_run.connection_id",
                "discovery_run.id",
            ],
            name="fk_discovery_staged_page_scope",
        ),
        sa.PrimaryKeyConstraint("run_id", "stage", "page"),
    )
    op.create_index(
        "ix_discovery_staged_page_tenant_run",
        "discovery_staged_page",
        ["tenant_id", "run_id"],
        unique=False,
    )


def downgrade():
    # 暂存页也是完整性证据，存在记录时不能通过回退迁移静默丢弃。
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM discovery_staged_page)"))
        .scalar_one()
    ):
        raise RuntimeError(
            "Directory staging evidence exists; downgrade is not allowed"
        )
    op.drop_index(
        "ix_discovery_staged_page_tenant_run", table_name="discovery_staged_page"
    )
    op.drop_table("discovery_staged_page")
    op.drop_constraint("uq_discovery_run_connection", "discovery_run", type_="unique")
