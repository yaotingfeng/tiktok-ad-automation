"""Local copyright scopes and explicitly user-supplied promotion links."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "manual_promotion_links"
down_revision = "automatic_mini_targets"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("preview_drama", "provider_pinyin", type_=sa.String(100))
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis','manual_link') AND applied_revision > 0",
    )
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind",
        "provider_connection",
        "kind IN ('wangyan','jiashu','other')",
    )
    op.drop_constraint(
        "ck_provider_connection_status", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_status",
        "provider_connection",
        "status IN ('pending','verifying','active','reauth_required','error','disabled','local')",
    )
    op.alter_column("provider_connection", "encrypted_credentials", nullable=True)
    op.add_column(
        "draft_input",
        sa.Column(
            "manual_link",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "promotion_link",
        sa.Column("source", sa.String(16), nullable=False, server_default="provider"),
    )
    op.create_check_constraint(
        "ck_promotion_link_source", "promotion_link", "source IN ('provider','manual')"
    )


def downgrade():
    # 不丢弃已经用于搭建的手动资料；有新数据时明确拒绝降级。
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM provider_connection WHERE kind='other') OR EXISTS(SELECT 1 FROM promotion_link WHERE source='manual') OR EXISTS(SELECT 1 FROM draft_input WHERE manual_link <> '{}'::jsonb) OR EXISTS(SELECT 1 FROM draft_mutation_request WHERE kind='manual_link')"
        )
    ).scalar():
        raise RuntimeError(
            "Manual link data exists; restore a verified backup instead of dropping it"
        )
    op.alter_column("preview_drama", "provider_pinyin", type_=sa.String(32))
    op.drop_constraint(
        "ck_draft_mutation_result", "draft_mutation_request", type_="check"
    )
    op.create_check_constraint(
        "ck_draft_mutation_result",
        "draft_mutation_request",
        "kind IN ('update','groups','minis') AND applied_revision > 0",
    )
    op.drop_constraint("ck_promotion_link_source", "promotion_link", type_="check")
    op.drop_column("promotion_link", "source")
    op.drop_column("draft_input", "manual_link")
    op.alter_column("provider_connection", "encrypted_credentials", nullable=False)
    op.drop_constraint(
        "ck_provider_connection_status", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_status",
        "provider_connection",
        "status IN ('pending','verifying','active','reauth_required','error','disabled')",
    )
    op.drop_constraint(
        "ck_provider_connection_kind", "provider_connection", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_connection_kind",
        "provider_connection",
        "kind IN ('wangyan','jiashu')",
    )
