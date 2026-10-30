"""按冻结范围和日期查找已有收尾任务，避免扫描所有历史任务。"""

import sqlalchemy as sa
from alembic import op

revision = "0064_closeout_job_scope"
down_revision = "0062_directory_pending_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 发布方先让所有应用正常退出，再在有限DDL/锁等待预算内创建普通索引。
    # 收尾去重必须包含所有状态；不改成只索引在途而重复安排已结束的收尾。
    # 路由和账户数组仍由原查询精确过滤，宽JSON不进入索引键。
    op.create_index(
        "ix_report_closeout_scope",
        "report_sync_job",
        ["tenant_id", "bc_id", "actor_id", "end_date"],
        postgresql_where=sa.text("priority = 0 AND start_date = end_date"),
    )


def downgrade() -> None:
    # 仅删除派生索引，不改任务、成功水位或已排队的消息。
    op.drop_index("ix_report_closeout_scope", table_name="report_sync_job")
