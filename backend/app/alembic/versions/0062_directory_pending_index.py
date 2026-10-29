"""按账户探测当前目录积压，避免遍历终态运行历史。"""

import sqlalchemy as sa
from alembic import op

revision = "0062_directory_pending_index"
down_revision = "0045_directory_type_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 发布方在全应用正常停写后创建；普通建索引会扫描运行表并阻挡并发写入。
    # 只索引当前在途，不收录宽JSON；冻结授权/完整查询的过滤仍由原SQL执行。
    op.create_index(
        "ix_ad_directory_pending_account",
        "ad_directory_run",
        ["tenant_id", "advertiser_id"],
        postgresql_where=sa.text(
            "status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')"
        ),
    )


def downgrade() -> None:
    # 只移除派生索引；运行、消息、发布版本和业务事实不回写。
    op.drop_index("ix_ad_directory_pending_account", table_name="ad_directory_run")
