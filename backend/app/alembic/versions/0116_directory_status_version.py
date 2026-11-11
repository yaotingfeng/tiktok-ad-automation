"""Cover account-scoped directory publication versions for status polling."""

import sqlalchemy as sa
from alembic import op

revision = "0116_directory_status_version"
down_revision = "0114_retire_material"
branch_labels = None
depends_on = None


def upgrade():
    # 发布窗口内创建；只索引完整发布，空快照的版本也保留在运行账本中。
    op.create_index(
        "ix_directory_status_version",
        "ad_directory_run",
        ["tenant_id", "bc_id", "advertiser_id", sa.text("published_version DESC")],
        postgresql_where=sa.text("status='COMPLETE' AND published_version IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_directory_status_version", table_name="ad_directory_run")
