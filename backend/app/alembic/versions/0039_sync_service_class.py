"""同步用途贯穿目录依赖，负证据保存真实身份签名。"""

import sqlalchemy as sa
from alembic import op

revision = "0039_sync_service_class"
down_revision = "0038_sync_work_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ad_directory_run",
        sa.Column("workload", sa.String(16), nullable=False, server_default="live"),
    )
    # 存量无来源证明的定向任务维持实时分类；全量确定为后台，不猜测BC/调用者。
    op.execute(
        "UPDATE ad_directory_run SET workload='background' WHERE COALESCE(jsonb_array_length(query->'ids'),0)=0 AND COALESCE(jsonb_array_length(query->'parent_ids'),0)=0"
    )
    op.create_check_constraint(
        "ck_ad_directory_workload",
        "ad_directory_run",
        "workload IN ('live','background')",
    )
    op.add_column(
        "material_link_scan",
        sa.Column("evidence_signature", sa.String(32), nullable=True),
    )

    op.add_column(
        "material_link_scan",
        sa.Column(
            "query_scope", sa.String(32), nullable=False, server_default="unscoped"
        ),
    )
    op.drop_constraint("material_link_scan_pkey", "material_link_scan", type_="primary")
    op.create_primary_key(
        "material_link_scan_pkey",
        "material_link_scan",
        ["tenant_id", "advertiser_id", "query_scope"],
    )


def downgrade():
    # 负缓存不是事实；降级丢弃分范围缓存，避免合并出未经证明的覆盖范围。
    op.execute("DELETE FROM material_link_scan")
    op.drop_constraint("material_link_scan_pkey", "material_link_scan", type_="primary")
    op.create_primary_key(
        "material_link_scan_pkey", "material_link_scan", ["tenant_id", "advertiser_id"]
    )
    op.drop_column("material_link_scan", "query_scope")
    op.drop_column("material_link_scan", "evidence_signature")
    op.drop_constraint("ck_ad_directory_workload", "ad_directory_run", type_="check")
    op.drop_column("ad_directory_run", "workload")
