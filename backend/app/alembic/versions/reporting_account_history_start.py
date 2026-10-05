"""Store the remote advertiser creation time for complete report backfills."""

import sqlalchemy as sa
from alembic import op

revision = "reporting_account_history_start"
down_revision = "20261005_preview_ad_material"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "advertiser_account",
        sa.Column("remote_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("advertiser_account", "remote_created_at")
