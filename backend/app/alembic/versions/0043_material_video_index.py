"""素材报表按视频读取使用位置：按租户与视频 ID 建索引。"""

from alembic import op

revision = "0043_material_video_index"
down_revision = "0042_drop_ad_material_main"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 素材维度只读取当天有数据的视频的使用位置；原先只有按账户/广告的索引，
    # 每次都要整租户扫描数百万条使用记录而超时。发布流程已冻结写入，直接建索引。
    op.create_index(
        "ix_ad_material_video",
        "ad_material_reference",
        ["tenant_id", "platform_material_id"],
        postgresql_where="complete",
    )


def downgrade() -> None:
    op.drop_index("ix_ad_material_video", table_name="ad_material_reference")
