"""按活跃业务身份寻找素材消息与待合并关联，避免扫描历史 outbox。"""

import sqlalchemy as sa
from alembic import op

revision = "0038_sync_work_indexes"
down_revision = "0037_realtime_sync"
branch_labels = None
depends_on = None


MATERIAL_TASKS = "task_name IN ('materials.upload_original','materials.verify_original','materials.prepare_target','materials.verify_target')"


def upgrade() -> None:
    op.create_index(
        "ix_ad_material_complete_name",
        "ad_material_reference",
        ["tenant_id", "advertiser_id", sa.text("md5(name)")],
        postgresql_where=sa.text("complete"),
    )
    op.create_index(
        "ix_dispatch_material_operation",
        "pending_dispatch",
        ["tenant_id", sa.text("(payload ->> 'operation_id')"), "task_name"],
        postgresql_where=sa.text(MATERIAL_TASKS),
    )
    op.create_index(
        "ix_report_link_pending_scope",
        "report_sync_job",
        ["tenant_id", "bc_id", "actor_id", "advertiser_ids"],
        postgresql_where=sa.text(
            "kind='material_links' AND status IN ('QUEUED','WAITING')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_ad_material_complete_name", table_name="ad_material_reference")
    op.drop_index("ix_report_link_pending_scope", table_name="report_sync_job")
    op.drop_index("ix_dispatch_material_operation", table_name="pending_dispatch")
