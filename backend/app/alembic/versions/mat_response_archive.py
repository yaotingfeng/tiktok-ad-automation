"""Archive complete source upload and reconciliation responses, encrypted.

Revision ID: mat_response_archive
Revises: mcp_multibc_runtime
"""

import sqlalchemy as sa
from alembic import op

revision = "mat_response_archive"
down_revision = "mcp_multibc_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "material_response_archive",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(128), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("advertiser_id", sa.String(128), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("body_bytes", sa.Integer(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("body_ciphertext", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id", "material_id", "advertiser_id", "operation_id"],
            [
                "material_asset_operation.tenant_id",
                "material_asset_operation.bc_id",
                "material_asset_operation.material_id",
                "material_asset_operation.advertiser_id",
                "material_asset_operation.id",
            ],
        ),
        sa.CheckConstraint("body_bytes >= 0", name="ck_material_response_size"),
        sa.CheckConstraint(
            "format IN ('mcp_tool_result_json','sdk_http_body')",
            name="ck_material_response_format",
        ),
    )
    op.create_index(
        "ix_material_response_lookup",
        "material_response_archive",
        ["tenant_id", "bc_id", "material_id", "received_at", "id"],
    )
    op.create_index(
        "ix_material_response_operation", "material_response_archive", ["operation_id"]
    )


def downgrade():
    op.drop_table("material_response_archive")
