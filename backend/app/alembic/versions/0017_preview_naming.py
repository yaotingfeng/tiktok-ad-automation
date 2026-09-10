"""Freeze provider naming inputs without rewriting historical advertising names."""

from alembic import op
import sqlalchemy as sa

revision = "0017_preview_naming"
down_revision = "r2_part_receipts"
branch_labels = None
depends_on = None


def upgrade():
    # 只加列，不回填冻结内容，也不关闭已有不可变触发器。
    op.add_column(
        "preview_drama",
        sa.Column("provider_pinyin", sa.String(32), nullable=False, server_default=""),
    )
    op.add_column(
        "preview_drama",
        sa.Column(
            "external_drama_id", sa.String(255), nullable=False, server_default=""
        ),
    )


def downgrade():
    op.drop_column("preview_drama", "external_drama_id")
    op.drop_column("preview_drama", "provider_pinyin")
