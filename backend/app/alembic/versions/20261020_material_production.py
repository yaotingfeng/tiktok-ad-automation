"""素材工具联合开户与免密绑定。"""

import sqlalchemy as sa
from alembic import op

revision = "0040_material_production"
down_revision = "0039_sync_service_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_production_binding",
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "administrator_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("key_ref", sa.String(128), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_ciphertext", sa.Text()),
        sa.Column("api_key_ciphertext", sa.Text()),
        sa.Column("company_id", sa.String(255)),
        sa.Column("external_user_id", sa.String(255)),
        sa.Column("api_key_id", sa.String(255)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_sequence", sa.Integer(), nullable=False),
        sa.Column("processed_sequence", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','ready','failed')",
            name="ck_material_production_status",
        ),
        sa.UniqueConstraint("request_id", name="uq_material_production_request"),
        sa.UniqueConstraint("username", name="uq_material_production_username"),
        sa.UniqueConstraint("company_id", name="uq_material_production_company"),
        sa.UniqueConstraint("external_user_id", name="uq_material_production_user"),
    )
    op.create_index(
        "ix_material_production_binding_next_attempt_at",
        "material_production_binding",
        ["next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_material_production_binding_next_attempt_at",
        table_name="material_production_binding",
    )
    op.drop_table("material_production_binding")
