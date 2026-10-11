"""Store provider application link defaults and clear obsolete draft link keys."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_provider_link_defaults"
down_revision = "0020_draft_delivery_mutation"
branch_labels = None
depends_on = None

# 已废弃字段：网眼推广名称、容量剧集与别名，均改为自动处理。
OBSOLETE = ("promote_name", "alias", "episodic_drama_id")
# 旧界面强制写入的默认值；清除后改走应用默认或按剧自动。
OLD_DEFAULTS = ("episode", "episode_seq", "card_point_episode", "free_episode_count")


def upgrade():
    op.add_column(
        "provider_application",
        sa.Column(
            "link_defaults",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_provider_application_link_defaults",
        "provider_application",
        "jsonb_typeof(link_defaults) = 'object'",
    )
    # 只处理尚未开始准备的草稿；已准备、已完成记录保持历史原样。
    obsolete = ", ".join(f"'{key}'" for key in OBSOLETE)
    op.execute(
        f"UPDATE build_draft SET link_config = link_config - ARRAY[{obsolete}] "
        "WHERE status = 'DRAFT'"
    )
    for key in OLD_DEFAULTS:
        op.execute(
            f"UPDATE build_draft SET link_config = link_config - '{key}' "
            f"WHERE status = 'DRAFT' AND link_config -> '{key}' IN "
            "('1'::jsonb, '\"1\"'::jsonb)"
        )
    # 空值与空串等同于未填写。
    op.execute(
        "UPDATE build_draft SET link_config = ("
        "SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) "
        "FROM jsonb_each(link_config) AS e(k, v) "
        "WHERE v <> 'null'::jsonb AND v <> '\"\"'::jsonb) WHERE status = 'DRAFT'"
    )


def downgrade():
    # 草稿清理不可逆；下降只移除新列。
    op.drop_constraint(
        "ck_provider_application_link_defaults", "provider_application", type_="check"
    )
    op.drop_column("provider_application", "link_defaults")
