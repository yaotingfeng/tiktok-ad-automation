"""Durable provider session refresh without holding locks across HTTP."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_provider_session_refresh"
down_revision = "0014_recovery_candidates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_session_refresh",
        sa.Column("connection_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("verification_token", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cooldown_rounds", sa.Integer(), nullable=False),
        sa.Column("unavailable_since", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("login_attempts", sa.Integer(), nullable=False),
        sa.Column("option_index", sa.Integer(), nullable=False),
        sa.Column("candidate_ciphertext", sa.String()),
        sa.Column("applications", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["provider_connection.tenant_id", "provider_connection.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "phase IN ('login','applications','options','complete','failed')",
            name="ck_provider_refresh_phase",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND login_attempts >= 0 AND option_index >= 0 AND cooldown_rounds >= 0",
            name="ck_provider_refresh_counters",
        ),
    )
    op.create_index(
        "ix_provider_session_refresh_tenant_id",
        "provider_session_refresh",
        ["tenant_id"],
    )

    # Resume old read/auth blocks through the existing exact-revision watchdog.
    # Old failed writes with an active effect require explicit reconciliation.
    op.execute("""UPDATE link_preparation_item
        SET status='retryable_error', resolved=jsonb_set(resolved,'{status}','"retryable_error"'::jsonb)
        WHERE status='blocked_auth' AND resolved->>'error_code'='provider_session_expired'
        AND resolved->'_work'->>'active_effect' IS NULL
        AND coalesce(resolved->'_work'->>'stage','') <> 'invalid'""")


def downgrade():
    op.drop_table("provider_session_refresh")
