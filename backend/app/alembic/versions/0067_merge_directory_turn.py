"""合并主线与已发布目录轮次迁移；不执行结构或数据变更。"""

revision = "0067_merge_directory_turn"
down_revision = ("0065_merge_closeout_carousel", "0066_directory_status_turn")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
