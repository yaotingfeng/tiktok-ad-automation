"""Allow the general strategy contract and nullable highest-value ROAS."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_general_ad_build_strategy"
down_revision = "mat_response_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Strategy versions and previews are immutable facts.  This migration only
    # changes the schema constraints for newly written rows; old JSON and
    # frozen preview values are projected by the application read boundary.
    op.alter_column("strategy_version", "target_roas", nullable=True)
    op.alter_column("build_preview", "target_roas", nullable=True)

    op.drop_constraint(
        "ck_strategy_version_values", "strategy_version", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_version_values",
        "strategy_version",
        "number > 0 AND budget > 0 AND budget != 'NaN'::numeric AND (target_roas IS NULL OR (target_roas > 0 AND target_roas != 'NaN'::numeric))",
    )
    op.drop_constraint(
        "ck_strategy_config_identity", "strategy_version", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_config_identity",
        "strategy_version",
        "coalesce((jsonb_typeof(config) = 'object' AND (config->>'budget')::numeric = budget AND (config->>'target_roas')::numeric IS NOT DISTINCT FROM target_roas AND config->>'copy_pool_version' = copy_pool_version_id::text), false)",
    )


def downgrade() -> None:
    # Refuse to make a nullable column mandatory while highest-value rows are
    # present.  No historical strategy or preview is rewritten for a rollback.
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM strategy_version WHERE target_roas IS NULL)"
        )
    ).scalar():
        raise RuntimeError("Cannot downgrade with nullable strategy target_roas rows")
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM build_preview WHERE target_roas IS NULL)"
        )
    ).scalar():
        raise RuntimeError("Cannot downgrade with nullable preview target_roas rows")

    op.drop_constraint(
        "ck_strategy_config_identity", "strategy_version", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_config_identity",
        "strategy_version",
        "coalesce((jsonb_typeof(config) = 'object' AND (config->>'budget')::numeric = budget AND (config->>'target_roas')::numeric = target_roas AND config->>'copy_pool_version' = copy_pool_version_id::text), false)",
    )
    op.drop_constraint(
        "ck_strategy_version_values", "strategy_version", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_version_values",
        "strategy_version",
        "number > 0 AND budget > 0 AND target_roas > 0 AND budget != 'NaN'::numeric AND target_roas != 'NaN'::numeric",
    )
    op.alter_column("build_preview", "target_roas", nullable=False)
    op.alter_column("strategy_version", "target_roas", nullable=False)
