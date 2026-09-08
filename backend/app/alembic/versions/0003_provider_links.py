"""Tenant-bound provider identities, reusable links and durable write scopes.

Revision ID: 0003_provider_links
Revises: 02c_directory
Create Date: 2026-09-09 03:45:24.351745

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003_provider_links"
down_revision = "02c_directory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("encrypted_credentials", sa.String(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verification_token", sa.Uuid(), nullable=True),
        sa.Column("verifying_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "kind IN ('wangyan','jiashu')", name="ck_provider_connection_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending','verifying','active','reauth_required','error','disabled')",
            name="ck_provider_connection_status",
        ),
        sa.CheckConstraint(
            "credential_version >= 0", name="ck_provider_connection_version"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_provider_connection_tenant_id"),
    )
    op.create_index(
        op.f("ix_provider_connection_tenant_id"),
        "provider_connection",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "provider_application",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "channel_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("tiktok_minis_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(channel_config) = 'object'",
            name="ck_provider_application_config",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["provider_connection.tenant_id", "provider_connection.id"],
            name="fk_provider_application_connection",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "connection_id",
            "external_id",
            name="uq_provider_application_external",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_provider_application_tenant_id"
        ),
    )
    op.create_index(
        "ix_provider_application_connection_id",
        "provider_application",
        ["tenant_id", "connection_id", "id"],
        unique=False,
    )
    op.create_table(
        "link_preparation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.String(length=255), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object'", name="ck_link_preparation_config"
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','ready','partial_ready','failed')",
            name="ck_link_preparation_status",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_link_preparation_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_link_preparation_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name="uq_link_preparation_request"
        ),
    )
    op.create_index(
        "ix_link_preparation_connection_id",
        "link_preparation",
        ["tenant_id", "connection_id", "id"],
        unique=False,
    )
    op.create_table(
        "provider_drama",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.String(length=255), nullable=False),
        sa.Column("external_drama_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id"],
            [
                "provider_application.tenant_id",
                "provider_application.connection_id",
                "provider_application.external_id",
            ],
            name="fk_provider_drama_application",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "connection_id",
            "application_id",
            "external_drama_id",
            name="uq_provider_drama_external",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "connection_id",
            "application_id",
            "id",
            name="uq_provider_drama_identity",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_provider_drama_tenant_id"),
    )
    op.create_table(
        "link_preparation_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("preparation_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("raw_input", sa.String(), nullable=False),
        sa.Column("resolved", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(resolved) = 'object'",
            name="ck_link_preparation_item_resolved",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolving','checking','creating','verifying','needs_resolution','blocked_auth','config_conflict','retryable_error','result_unknown','failed','ready')",
            name="ck_link_preparation_item_status",
        ),
        sa.CheckConstraint("line_no > 0", name="ck_link_preparation_item_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "preparation_id"],
            ["link_preparation.tenant_id", "link_preparation.id"],
            name="fk_link_preparation_item_preparation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "preparation_id", "line_no", name="uq_link_preparation_item_line"
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_link_preparation_item_tenant_id"
        ),
    )
    op.create_index(
        "ix_link_preparation_item_page",
        "link_preparation_item",
        ["tenant_id", "preparation_id", "line_no", "id"],
        unique=False,
    )
    op.create_table(
        "promotion_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("reuse_key", sa.String(length=64), nullable=False),
        sa.Column("drama_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.String(length=255), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("protected_base", sa.String(), nullable=True),
        sa.Column(
            "attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object' AND jsonb_typeof(attribution) = 'object'",
            name="ck_promotion_link_json",
        ),
        sa.CheckConstraint(
            "status != 'ready' OR (url IS NOT NULL AND length(btrim(url)) > 0 AND protected_base IS NOT NULL AND verified_at IS NOT NULL)",
            name="ck_promotion_link_ready",
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready','superseded','invalid','result_unknown','failed')",
            name="ck_promotion_link_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_promotion_link_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "application_id", "drama_id"],
            [
                "provider_drama.tenant_id",
                "provider_drama.connection_id",
                "provider_drama.application_id",
                "provider_drama.id",
            ],
            name="fk_promotion_link_drama_identity",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_promotion_link_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "reuse_key", "version", name="uq_promotion_link_version"
        ),
    )
    op.create_index(
        "ix_promotion_link_drama",
        "promotion_link",
        ["tenant_id", "connection_id", "application_id", "drama_id"],
        unique=False,
    )
    op.create_index(
        "uq_promotion_link_current_ready",
        "promotion_link",
        ["tenant_id", "reuse_key"],
        unique=True,
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_table(
        "provider_remote_scope",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("active_item_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "(status = 'idle' AND active_item_id IS NULL) OR (status IN ('held','result_unknown') AND active_item_id IS NOT NULL)",
            name="ck_provider_remote_scope_owner",
        ),
        sa.CheckConstraint(
            "status IN ('idle','held','result_unknown')",
            name="ck_provider_remote_scope_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "active_item_id"],
            ["link_preparation_item.tenant_id", "link_preparation_item.id"],
            name="fk_provider_remote_scope_active_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_provider_remote_scope_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "scope_key", name="uq_provider_remote_scope_key"
        ),
    )
    op.create_index(
        "ix_provider_remote_scope_active_item",
        "provider_remote_scope",
        ["tenant_id", "active_item_id"],
        unique=False,
    )
    op.create_table(
        "provider_effect",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("remote_scope_key", sa.String(length=255), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_token", sa.Uuid(), nullable=True),
        sa.Column("remote_id", sa.String(length=255), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(result) = 'object'", name="ck_provider_effect_result"
        ),
        sa.CheckConstraint(
            "status != 'sending' OR attempt_token IS NOT NULL",
            name="ck_provider_effect_sending_token",
        ),
        sa.CheckConstraint(
            "status IN ('pending','confirmed_absent','sending','result_unknown','succeeded','failed')",
            name="ck_provider_effect_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "remote_scope_key"],
            ["provider_remote_scope.tenant_id", "provider_remote_scope.scope_key"],
            name="fk_provider_effect_remote_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_provider_effect_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "remote_scope_key",
            "step",
            "request_digest",
            name="uq_provider_effect_intent",
        ),
    )


def downgrade():
    op.drop_table("provider_effect")
    op.drop_index(
        "ix_provider_remote_scope_active_item", table_name="provider_remote_scope"
    )
    op.drop_table("provider_remote_scope")
    op.drop_index(
        "uq_promotion_link_current_ready",
        table_name="promotion_link",
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.drop_index("ix_promotion_link_drama", table_name="promotion_link")
    op.drop_table("promotion_link")
    op.drop_index("ix_link_preparation_item_page", table_name="link_preparation_item")
    op.drop_table("link_preparation_item")
    op.drop_table("provider_drama")
    op.drop_index("ix_link_preparation_connection_id", table_name="link_preparation")
    op.drop_table("link_preparation")
    op.drop_index(
        "ix_provider_application_connection_id", table_name="provider_application"
    )
    op.drop_table("provider_application")
    op.drop_index(
        op.f("ix_provider_connection_tenant_id"), table_name="provider_connection"
    )
    op.drop_table("provider_connection")
