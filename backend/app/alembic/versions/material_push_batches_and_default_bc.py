"""Batch material push and tenant default BC.

Revision ID: material_push
Revises: audience_targeting
Create Date: 2026-09-29 10:55:31.746892

"""

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "material_push"
down_revision = "audience_targeting"
branch_labels = None
depends_on = None


def upgrade():
    # 名称用于外部归属，不自动改名或任取历史重名记录。
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM tenant GROUP BY name HAVING count(*) > 1 LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("存在重名租户，请先由管理员消除歧义后再迁移")
    op.create_table(
        "material_push_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_name", sqlmodel.sql.sqltypes.AutoString(length=120), nullable=False
        ),
        sa.Column(
            "bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "key_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_digest",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
        ),
        sa.Column(
            "frozen_route", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_membership.tenant_id", "tenant_membership.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "bc_id"],
            ["tenant_bc.tenant_id", "tenant_bc.bc_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_id", "request_id", name="uq_push_batch_request"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_push_batch_scope"),
    )
    op.create_table(
        "pushed_material",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "external_id", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False
        ),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("current_material_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("latest_revision > 0", name="ck_pushed_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "current_material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("tenant_id", "external_id"),
    )
    op.create_table(
        "material_push_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column(
            "external_id", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "file_name", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False
        ),
        sa.Column("url_ciphertext", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False
        ),
        sa.Column(
            "error_code", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','validating','imported','failed')",
            name="ck_push_item_status",
        ),
        sa.CheckConstraint(
            "revision > 0 AND attempts >= 0", name="ck_push_item_numbers"
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"],
            ["pending_dispatch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["material_push_batch.tenant_id", "material_push_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "external_id"],
            ["pushed_material.tenant_id", "pushed_material.external_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "external_id", "revision", name="uq_push_item_revision"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_push_item_scope"),
    )
    op.create_index(
        "ix_push_item_repair",
        "material_push_item",
        ["status", "claimed_until", "id"],
        unique=False,
    )
    op.create_table(
        "external_material_source",
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("etag", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["material_push_item.tenant_id", "material_push_item.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "material_id"],
            ["material_file.tenant_id", "material_file.id"],
        ),
        sa.PrimaryKeyConstraint("material_id"),
    )
    op.add_column(
        "tenant",
        sa.Column(
            "default_bc_id", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True
        ),
    )
    op.create_unique_constraint("uq_tenant_name", "tenant", ["name"])
    op.create_foreign_key(
        "fk_tenant_default_bc",
        "tenant",
        "tenant_bc",
        ["id", "default_bc_id"],
        ["tenant_id", "bc_id"],
        use_alter=True,
    )
    # ### end Alembic commands ###


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM material_push_batch LIMIT 1"))
        .first()
    ):
        raise RuntimeError("已有外部素材批次，禁止丢弃推送历史的降级")
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM tenant WHERE default_bc_id IS NOT NULL LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("已有默认 BC 配置，禁止静默丢弃")
    op.drop_constraint("fk_tenant_default_bc", "tenant", type_="foreignkey")
    op.drop_constraint("uq_tenant_name", "tenant", type_="unique")
    op.drop_column("tenant", "default_bc_id")
    op.drop_table("external_material_source")
    op.drop_index("ix_push_item_repair", table_name="material_push_item")
    op.drop_table("material_push_item")
    op.drop_table("pushed_material")
    op.drop_table("material_push_batch")
    # ### end Alembic commands ###
