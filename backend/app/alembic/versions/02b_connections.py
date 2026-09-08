"""Tenant-bound TikTok credentials and one-use OAuth attempts.

Revision ID: 02b_connections
Revises: 02a_tenants
"""
from alembic import op
import sqlalchemy as sa

revision = "02b_connections"
down_revision = "02a_tenants"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tiktok_connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("credential_ciphertext", sa.String(), nullable=True),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tiktok_connection_tenant_id"),
        sa.CheckConstraint("status IN ('PENDING_AUTH','DISCOVERING','ACTIVE','REAUTH_REQUIRED','ERROR','DISABLED')", name="ck_tiktok_connection_status"),
        sa.CheckConstraint("credential_version >= 0", name="ck_tiktok_connection_version"),
    )
    op.create_index("ix_tiktok_connection_tenant_id", "tiktok_connection", ["tenant_id"])
    op.create_table(
        "authorization_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("base_credential_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("candidate_ciphertext", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "connection_id"], ["tiktok_connection.tenant_id", "tiktok_connection.id"], name="fk_authorization_attempt_tenant_connection"),
        sa.CheckConstraint("base_credential_version >= 0", name="ck_authorization_attempt_base_version"),
        sa.CheckConstraint("status IN ('PENDING','CLAIMED','CANDIDATE_READY','RESULT_UNKNOWN','CANCELLED','FAILED','ACCEPTED')", name="ck_authorization_attempt_status"),
    )
    op.create_index("ix_authorization_attempt_tenant_id", "authorization_attempt", ["tenant_id"])
    op.create_index("ix_authorization_attempt_connection_id", "authorization_attempt", ["connection_id"])


def downgrade():
    op.drop_table("authorization_attempt")
    op.drop_table("tiktok_connection")
