"""scoped advertising management contracts and independent permissions

Revision ID: ad_management
Revises: reporting_queries
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ad_management"
down_revision = "reporting_queries"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # 复合键把租户/BC/账户/连接及当前授权代数绑定在同一条证据上。
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
        sa.CheckConstraint("authorization_revision >= 0 AND binding_revision >= 0", name="ck_management_capability_revisions"),
        sa.CheckConstraint("length(trim(adapter_contract_revision)) > 0", name="ck_management_capability_contract"),
        sa.CheckConstraint("operation IN ('update_roas','update_budget','set_status','set_material_status','ads_manage')", name="ck_management_capability_operation"),
        sa.CheckConstraint("entity_kind IN ('campaign','adgroup','ad','creative','material','account')", name="ck_management_capability_entity_kind"),
        sa.CheckConstraint("state IN ('VERIFIED','UNKNOWN','REVOKED')", name="ck_management_capability_state"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]),
        sa.ForeignKeyConstraint(["tenant_id", "advertiser_id"], ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"]),
        sa.ForeignKeyConstraint(["tenant_id", "connection_id"], ["tiktok_connection.tenant_id", "tiktok_connection.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "bc_id", "advertiser_id", "connection_id", "authorization_revision", "binding_revision", "adapter_contract_revision", "operation", "entity_kind", name="uq_management_capability_key"),
    )
    op.create_index("ix_management_capability_tenant_id", "management_capability", ["tenant_id"], unique=False)
    op.create_index("ix_management_capability_lookup", "management_capability", ["tenant_id", "bc_id", "advertiser_id", "connection_id", "state"], unique=False)

    # 新增复合外键前补齐冻结选择的租户唯一键，升级不改写既有数据。
    op.create_unique_constraint("uq_frozen_selection_tenant_id", "frozen_selection", ["tenant_id", "id"])

    op.create_table(
        "management_preview",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("selection_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("mutation", JSONB, nullable=False),
        sa.Column("route", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.CheckConstraint("expires_at > created_at", name="ck_management_preview_expiry"),
        sa.CheckConstraint("status IN ('PREPARING','READY','EXPIRED','OBSOLETE')", name="ck_management_preview_status"),
        sa.ForeignKeyConstraint(["tenant_id", "bc_id"], ["tenant_bc.tenant_id", "tenant_bc.bc_id"]),
        sa.ForeignKeyConstraint(["tenant_id", "selection_id"], ["frozen_selection.tenant_id", "frozen_selection.id"], name="fk_management_preview_selection"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_management_preview_tenant_id"),
        sa.UniqueConstraint("tenant_id", "bc_id", "id", name="uq_management_preview_scope_id"),
    )
    op.create_index("ix_management_preview_tenant_id", "management_preview", ["tenant_id"], unique=False)
    op.create_index("ix_management_preview_owner_expiry", "management_preview", ["tenant_id", "bc_id", "actor_id", "expires_at"], unique=False)

    op.create_table(
        "management_preview_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ref", JSONB, nullable=False),
        sa.Column("material_use", JSONB, nullable=True),
        sa.Column("parent_ref", JSONB, nullable=True),
        sa.Column("grouping_revision", sa.Integer(), nullable=False),
        sa.Column("membership_digest", sa.String(length=64), nullable=True),
        sa.Column("capability", JSONB, nullable=False),
        sa.Column("original_value", sa.String(length=128), nullable=True),
        sa.Column("final_value", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("execution_result", sa.String(length=16), nullable=False),
        sa.Column("observation_state", sa.String(length=32), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=True),
        sa.Column("request_attribution", sa.String(length=64), nullable=True),
        sa.CheckConstraint("execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED')", name="ck_management_preview_item_result"),
        sa.ForeignKeyConstraint(["tenant_id", "preview_id"], ["management_preview.tenant_id", "management_preview.id"], ondelete="CASCADE", name="fk_management_preview_item_preview"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_management_preview_item_tenant_id"),
    )
    op.create_index("ix_management_preview_item_order", "management_preview_item", ["tenant_id", "preview_id", "position"], unique=False)

    op.create_table(
        "management_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("bc_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("preview_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("route", JSONB, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('PREPARING','READY','QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','NEEDS_REVIEW','CANCELLED')", name="ck_management_task_status"),
        sa.ForeignKeyConstraint(["tenant_id", "bc_id", "preview_id"], ["management_preview.tenant_id", "management_preview.bc_id", "management_preview.id"], name="fk_management_task_preview"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_management_task_tenant_id"),
        sa.UniqueConstraint("tenant_id", "actor_id", "idempotency_key", name="uq_management_task_idempotency"),
    )
    op.create_index("ix_management_task_tenant_id", "management_task", ["tenant_id"], unique=False)
    op.create_index("ix_management_task_scope_status", "management_task", ["tenant_id", "bc_id", "status", "created_at"], unique=False)

    op.create_table(
        "management_task_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("preview_item_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ref", JSONB, nullable=False),
        sa.Column("material_use", JSONB, nullable=True),
        sa.Column("original_value", sa.String(length=128), nullable=True),
        sa.Column("final_value", sa.String(length=128), nullable=True),
        sa.Column("execution_result", sa.String(length=16), nullable=False),
        sa.Column("observation_state", sa.String(length=32), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=True),
        sa.Column("request_attribution", sa.String(length=64), nullable=True),
        sa.Column("grouping_revision", sa.Integer(), nullable=False),
        sa.Column("parent_ref", JSONB, nullable=True),
        sa.Column("capability", JSONB, nullable=False),
        sa.CheckConstraint("execution_result IN ('PENDING','ACCEPTED','REJECTED','NOT_SENT','UNKNOWN','NO_CHANGE','CONFLICT','UNSUPPORTED')", name="ck_management_task_item_result"),
        sa.ForeignKeyConstraint(["tenant_id", "task_id"], ["management_task.tenant_id", "management_task.id"], ondelete="CASCADE", name="fk_management_task_item_task"),
        sa.ForeignKeyConstraint(["tenant_id", "preview_item_id"], ["management_preview_item.tenant_id", "management_preview_item.id"], name="fk_management_task_item_preview_item"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_management_task_item_tenant_id"),
    )
    op.create_index("ix_management_task_item_status", "management_task_item", ["tenant_id", "task_id", "execution_result"], unique=False)

    op.create_table(
        "management_request_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_item_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("request_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("response", JSONB, nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.CheckConstraint("outcome IN ('ACCEPTED','REJECTED','NOT_SENT','UNKNOWN')", name="ck_management_request_attempt_outcome"),
        sa.ForeignKeyConstraint(["tenant_id", "task_item_id"], ["management_task_item.tenant_id", "management_task_item.id"], ondelete="CASCADE", name="fk_management_request_attempt_item"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_management_request_attempt_tenant_id"),
    )
    op.create_index("ix_management_request_attempt_item", "management_request_attempt", ["tenant_id", "task_item_id", "attempt"], unique=False)

    op.create_table(
        "management_receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response", JSONB, nullable=False),
        sa.CheckConstraint("outcome IN ('ACCEPTED','REJECTED','NOT_SENT','UNKNOWN')", name="ck_management_receipt_outcome"),
        sa.ForeignKeyConstraint(["tenant_id", "attempt_id"], ["management_request_attempt.tenant_id", "management_request_attempt.id"], ondelete="CASCADE", name="fk_management_receipt_attempt"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("management_receipt")
    op.drop_index("ix_management_request_attempt_item", table_name="management_request_attempt")
    op.drop_table("management_request_attempt")
    op.drop_index("ix_management_task_item_status", table_name="management_task_item")
    op.drop_table("management_task_item")
    op.drop_index("ix_management_task_scope_status", table_name="management_task")
    op.drop_index("ix_management_task_tenant_id", table_name="management_task")
    op.drop_table("management_task")
    op.drop_index("ix_management_preview_item_order", table_name="management_preview_item")
    op.drop_table("management_preview_item")
    op.drop_index("ix_management_preview_owner_expiry", table_name="management_preview")
    op.drop_index("ix_management_preview_tenant_id", table_name="management_preview")
    op.drop_table("management_preview")
    op.drop_constraint("uq_frozen_selection_tenant_id", "frozen_selection", type_="unique")
    op.drop_index("ix_management_capability_lookup", table_name="management_capability")
    op.drop_index("ix_management_capability_tenant_id", table_name="management_capability")
    op.drop_table("management_capability")
