"""Freeze the provider display drama ID used in new advertising names."""

import sqlalchemy as sa
from alembic import op

revision = "preview_display_drama_id"
down_revision = "build_batching"
branch_labels = None
depends_on = None


def upgrade():
    # 历史预览不回填，避免把可变目录值写成当时的命名事实；冻结广告继续读原名称。
    op.add_column(
        "preview_drama",
        sa.Column(
            "display_drama_id", sa.String(255), nullable=False, server_default=""
        ),
    )


def downgrade():
    op.drop_column("preview_drama", "display_drama_id")
