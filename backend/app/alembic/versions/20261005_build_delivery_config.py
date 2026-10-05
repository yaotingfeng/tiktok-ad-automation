"""Store per-build delivery scheduling separately from strategy versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261005_build_delivery_config"
down_revision = "20261005_preview_copy_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "build_draft",
        sa.Column(
            "delivery_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("build_draft", "delivery_config", server_default=None)


def downgrade() -> None:
    op.drop_column("build_draft", "delivery_config")
