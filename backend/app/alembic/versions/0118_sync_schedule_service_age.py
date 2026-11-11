"""按租户、接口、工作类的服务年龄覆盖调度头发现，不改变就绪条件或公平权重。"""

import sqlalchemy as sa
from alembic import op

revision = "0118_sync_schedule_service_age"
down_revision = "0117_directory_status_version"
branch_labels = None
depends_on = None


def upgrade():
    # 正常冻结窗口内创建；两个 NULLS FIRST 与现有候选查询顺序完全一致。
    op.create_index(
        "ix_sync_schedule_service_age",
        "sync_schedule",
        [
            "tenant_id",
            "physical_endpoint",
            "work_class",
            sa.text("last_dispatched_at ASC NULLS FIRST"),
            sa.text("oldest_pending_at ASC NULLS FIRST"),
        ],
        postgresql_include=["scope_id", "next_eligible_at", "leased_until"],
        postgresql_where=sa.text("active IS TRUE"),
    )


def downgrade():
    op.drop_index("ix_sync_schedule_service_age", table_name="sync_schedule")
