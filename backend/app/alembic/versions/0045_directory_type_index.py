"""按租户、账户、广告类型短探测目录，避免逐对象回表判断已用类型。"""

from alembic import op

revision = "0045_directory_type_index"
down_revision = "0044_sync_probe_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 按获批的冻结写入流程创建；广告类型必须是索引键，缺失类型才能直接排除。
    # 任意层级及已删除对象都表示使用过，不能添加状态、层级或日期部分谓词。
    op.create_index(
        "ix_ad_object_type", "ad_object", ["tenant_id", "advertiser_id", "ad_type"]
    )


def downgrade() -> None:
    # 仅移除查询索引，不修改已同步目录或发布水位。
    op.drop_index("ix_ad_object_type", table_name="ad_object")
