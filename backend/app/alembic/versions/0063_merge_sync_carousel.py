"""合并独立生产同步索引与轮播功能迁移，不重写已发布父链。"""

revision = "0063_merge_sync_carousel"
down_revision = ("0061_carousel_ad_readiness", "0062_directory_pending_index")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 生产0062分支升级main时仍须执行0046–0061；本合并节点自身不改业务结构。
    pass


def downgrade() -> None:
    # 仅拆回两个父head，不移除任一父迁移创建的业务结构或索引。
    pass
