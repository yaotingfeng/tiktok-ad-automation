"""Merge the delivery-config and reporting migration branches."""

revision = "20261005_merge_delivery_heads"
down_revision = ("20261005_build_delivery_config", "reporting_account_history_start")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
