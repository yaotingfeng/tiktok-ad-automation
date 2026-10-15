"""Reference actors by user instead of tenant membership."""

from alembic import op

revision = "0030_actor_user_reference"
down_revision = "0029_material_post_link"
branch_labels = None
depends_on = None

# 平台管理员（超级管理员）按权限可代管任意租户，但不是该租户成员。
# 这些表原先要求 (tenant_id, actor_id) 必须存在于 tenant_membership，
# 导致平台管理员代租户授权、发现账户或上传素材后，后台写入因外键失败。
# 改为只关联用户，租户权限由 require_tenant 在每次执行时重新校验。
TABLES = (
    "ad_directory_run",
    "ingest_session",
    "material_push_batch",
    "mcp_authorization_attempt",
    "original_use",
)


def upgrade() -> None:
    for table in TABLES:
        op.drop_constraint(
            f"{table}_tenant_id_actor_id_fkey", table, type_="foreignkey"
        )
        op.create_foreign_key(
            f"{table}_actor_id_fkey", table, "user", ["actor_id"], ["id"]
        )


def downgrade() -> None:
    # 回退前须确认没有非成员操作人的行，否则重建成员外键会失败。
    for table in TABLES:
        op.drop_constraint(f"{table}_actor_id_fkey", table, type_="foreignkey")
        op.create_foreign_key(
            f"{table}_tenant_id_actor_id_fkey",
            table,
            "tenant_membership",
            ["tenant_id", "actor_id"],
            ["tenant_id", "user_id"],
        )
