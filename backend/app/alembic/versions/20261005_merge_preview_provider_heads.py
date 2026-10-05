"""合并预览策略与版权方类型两条迁移分支。"""

revision = "20261005_merge_preview_provider_heads"
down_revision = ("20261005_preview_ad_material_strategy", "provider_kinds_expansion")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """合并迁移图，不执行任何数据或结构操作。"""


def downgrade() -> None:
    """回退到两个父 head，同样不执行数据或结构操作。"""
