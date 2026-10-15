"""跨连接测试提交了数据；按外键依赖逆序删除本测试租户写入的行。"""

from uuid import UUID

from sqlalchemy import text

from app.core.db import engine

# 子表在前。只列执行器测试会写入的表，避免误删共享基线数据。
_TABLES = (
    "pending_dispatch",
    "dispatch_tenant_cursor",
    "automation_action_ledger",
    "automation_confirmation",
    "automation_rule_hit",
    "automation_rule_run",
    "automation_rule",
    "automation_tenant_setting",
    "management_task_item",
    "management_task",
    "management_preview_item",
    "management_preview",
    "frozen_selection",
    "ad_object",
    "bc_default_route",
    "connection_authorization",
    "bc_connection_binding",
    "bc_account_access",
    "advertiser_account",
    "tenant_bc",
    "tiktok_connection",
    "tenant_membership",
)


def purge_tenant(tenant_id: UUID, actor_id: UUID) -> None:
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(
                text(f'DELETE FROM "{table}" WHERE tenant_id = :t'), {"t": tenant_id}
            )
        connection.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": tenant_id})
        connection.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": actor_id})
