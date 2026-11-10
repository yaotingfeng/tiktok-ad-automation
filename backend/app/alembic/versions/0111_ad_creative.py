"""Persist tenant/account scoped immutable creative identities."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0111_ad_creative"
down_revision = "0110_campaign_copy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_creative",
        sa.Column("tenant_id", sa.Uuid(), primary_key=True),
        sa.Column("advertiser_id", sa.String(128), primary_key=True),
        sa.Column("creative_id", sa.String(128), primary_key=True),
        sa.Column("ad_remote_id", sa.String(128), nullable=False),
        sa.Column("ad_material_id", sa.String(128)),
        sa.Column("material_type", sa.String(64)),
        sa.Column("platform_material_id", sa.String(128)),
        sa.Column("assets", postgresql.JSONB(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("operation_status", sa.String()),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("identity_status", sa.String(16), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "advertiser_id"], ["advertiser_account.tenant_id", "advertiser_account.advertiser_id"]),
        sa.CheckConstraint("published_version > 0", name="ck_ad_creative_version"),
        sa.CheckConstraint("identity_status IN ('RESOLVED','COMPOSITE','UNRESOLVED','CONFLICT')", name="ck_ad_creative_identity_status"),
        sa.CheckConstraint("jsonb_typeof(assets) = 'array'", name="ck_ad_creative_assets"),
    )
    op.create_index("ix_ad_creative_material", "ad_creative", ["tenant_id", "platform_material_id", "advertiser_id"])
    op.create_index("ix_ad_creative_parent", "ad_creative", ["tenant_id", "advertiser_id", "ad_remote_id"])


def downgrade() -> None:
    op.drop_table("ad_creative")
