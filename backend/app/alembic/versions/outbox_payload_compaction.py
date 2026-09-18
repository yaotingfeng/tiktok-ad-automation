"""压缩已结算投递正文，同时保留稳定身份和配置摘要。"""

import sqlalchemy as sa
from alembic import op

revision = "outbox_payload_compaction"
down_revision = "material_batch_candidate_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pending_dispatch",
        sa.Column("payload_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "pending_dispatch",
        sa.Column("compacted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dispatch_compaction",
        "pending_dispatch",
        ["published_at", "id"],
        postgresql_where=(
            "published_at IS NOT NULL AND compacted_at IS NULL "
            "AND task_name IN ('builds.execute_step','builds.execute_unit')"
        ),
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(
                SELECT 1 FROM pending_dispatch WHERE compacted_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot restore compacted dispatch payloads';
            END IF;
        END $$;
    """)
    op.drop_index("ix_dispatch_compaction", table_name="pending_dispatch")
    op.drop_column("pending_dispatch", "compacted_at")
    op.drop_column("pending_dispatch", "payload_digest")
