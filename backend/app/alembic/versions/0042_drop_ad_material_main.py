"""删除目录接口无来源的主素材字段；报表/帖子身份保留。"""

import sqlalchemy as sa
from alembic import op

revision = "0042_drop_ad_material_main"
down_revision = "0041_rule_frequency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 目录只提供广告内素材与视频/帖子身份；主素材属于报表链路，不应留空列冒充证据。
    op.drop_constraint(
        "ck_ad_material_main_identity", "ad_material_reference", type_="check"
    )
    op.drop_column("ad_material_reference", "main_material_id")
    op.drop_column("ad_material_reference", "main_material_type")


def downgrade() -> None:
    # 仅恢复旧结构及成对约束；目录本无来源，不能从报表猜填或伪造旧值。
    op.add_column(
        "ad_material_reference",
        sa.Column("main_material_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "ad_material_reference",
        sa.Column("main_material_type", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        "ck_ad_material_main_identity",
        "ad_material_reference",
        "(main_material_id IS NULL) = (main_material_type IS NULL)",
    )
