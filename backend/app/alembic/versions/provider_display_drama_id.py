"""保留版权方展示编号，旧网眼记录只按同范围归因事实补齐。"""

import sqlalchemy as sa
from alembic import op

revision = "provider_display_drama_id"
down_revision = "manual_promotion_links"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_drama", sa.Column("display_drama_id", sa.String(255)))
    # 多份历史链接对数字 ID 有分歧时不猜测，也不改写取链接口使用的长 ID。
    op.execute("""
        UPDATE provider_drama AS d SET display_drama_id = known.numeric_id
        FROM (
            SELECT l.tenant_id, l.connection_id, l.application_id, l.drama_id,
                   min(l.attribution->>'drama_int_id') AS numeric_id
            FROM promotion_link l
            JOIN provider_connection c ON c.id=l.connection_id AND c.tenant_id=l.tenant_id
            WHERE c.kind='wangyan'
              AND l.attribution->>'drama_int_id' ~ '^[1-9][0-9]{0,254}$'
            GROUP BY l.tenant_id, l.connection_id, l.application_id, l.drama_id
            HAVING count(DISTINCT l.attribution->>'drama_int_id')=1
        ) known
        WHERE d.id=known.drama_id AND d.tenant_id=known.tenant_id
          AND d.connection_id=known.connection_id AND d.application_id=known.application_id
    """)


def downgrade():
    op.drop_column("provider_drama", "display_drama_id")
