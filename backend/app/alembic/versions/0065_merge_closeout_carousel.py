"""合并收尾索引与既有功能迁移，保持独立生产父链。"""

revision = "0065_merge_closeout_carousel"
down_revision = ("0063_merge_sync_carousel", "0064_closeout_job_scope")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 生产0064升级main仍须执行未发布功能迁移；本节点自身不执行DDL。
    pass


def downgrade() -> None:
    # 只拆回两个父head，不删除父迁移的索引或功能数据。
    pass
