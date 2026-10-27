"""为目录近期成功探测和正消耗事实健康扫描补齐部分索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0044_sync_probe_indexes"
down_revision = "0043_material_video_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 遵循仓库迁移的冻结写入流程；父表普通 CREATE INDEX 会递归覆盖全部分区。
    # 不能在分区父表使用 CREATE INDEX CONCURRENTLY，也不把now()放进索引谓词。
    op.create_index(
        "ix_ad_directory_recent_success",
        "ad_directory_run",
        ["tenant_id", "advertiser_id", "completed_at"],
        postgresql_where=sa.text(
            "status = 'COMPLETE' AND CAST(query->>'include_deleted' AS boolean) IS TRUE"
        ),
    )
    op.create_index(
        "ix_ad_report_day_spending",
        "ad_report_day",
        ["tenant_id", "stat_date", "advertiser_id"],
        postgresql_include=["timezone", "ad_id", "adgroup_id", "campaign_id"],
        postgresql_where=sa.text("spend > 0"),
    )
    op.create_index(
        "ix_material_report_day_spending",
        "material_report_day",
        ["tenant_id", "stat_date", "advertiser_id"],
        postgresql_include=["timezone", "main_material_id"],
        postgresql_where=sa.text("spend > 0"),
    )


def downgrade() -> None:
    # 仅移除派生索引；不改事实/水位/运行状态，不能等同回退整套业务数据。
    op.drop_index("ix_material_report_day_spending", table_name="material_report_day")
    op.drop_index("ix_ad_report_day_spending", table_name="ad_report_day")
    op.drop_index("ix_ad_directory_recent_success", table_name="ad_directory_run")
