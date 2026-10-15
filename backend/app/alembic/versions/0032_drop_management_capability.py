"""Drop management_capability: management permission now follows can_build/scopes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032_drop_management_capability"
down_revision = "0031_cancel_stacked_dir_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 广告管理写权直接由账户同步时核实的搭建权限（及创意授权）判定，
    # 单独的能力记录不再被任何代码读取，连同其中的证据一起删除。
    op.drop_table("management_capability")


def downgrade() -> None:
    # 只恢复空表结构；旧能力证据需要重新发布，不在此回填。
    op.create_table(
        "management_capability",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("advertiser_id", sa.String(length=128), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_revision", sa.Integer(), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("adapter_contract_revision", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("entity_kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bc_id",
            "advertiser_id",
            "connection_id",
            "authorization_revision",
            "binding_revision",
            "adapter_contract_revision",
            "operation",
            "entity_kind",
            name="uq_management_capability_key",
        ),
    )
