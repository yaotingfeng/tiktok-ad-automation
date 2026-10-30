"""操作记录的集合式读取权限；分页和计数共用同一数据库过滤条件。"""

from datetime import UTC, datetime

from sqlalchemy import Text, and_, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import col, select

from app.core.context import TenantContext
from app.modules.accounts.access import usable_grants
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.models import TikTokConnection

from .models import ManagementTask, ManagementTaskItem


def readable_history(context: TenantContext, bc_id: str):
    """保留原连接及授权/绑定代数，任意一个目标失权时隐藏整笔任务。

    与 verify_route(read) 一致：复用账户目录条件并补齐冻结路由和明确读取授权，
    不能仅以租户/BC 可见代替账户权限，也不能借当前默认连接读取旧任务。
    """
    now = datetime.now(UTC)
    grants = usable_grants(
        tenant_id=context.tenant_id, bc_id=bc_id, action="read"
    ).subquery()
    readable = (
        select(
            grants.c.advertiser_id,
            func.jsonb_build_object(
                "tenant_id",
                cast(col(TikTokConnection.tenant_id), Text),
                "bc_id",
                col(BCConnectionBinding.bc_id),
                "connection_id",
                cast(col(TikTokConnection.id), Text),
                "channel",
                col(TikTokConnection.kind),
                "authorization_revision",
                col(TikTokConnection.authorization_revision),
                "adapter_contract_revision",
                col(TikTokConnection.adapter_contract_revision),
                "binding_revision",
                col(BCConnectionBinding.revision),
            )
            .cast(JSONB)
            .label("route"),
        )
        .select_from(grants)
        .join(TikTokConnection, col(TikTokConnection.id) == grants.c.connection_id)
        .join(
            BCConnectionBinding,
            and_(
                col(BCConnectionBinding.tenant_id) == grants.c.tenant_id,
                col(BCConnectionBinding.bc_id) == grants.c.bc_id,
                col(BCConnectionBinding.connection_id) == grants.c.connection_id,
                col(BCConnectionBinding.kind) == col(TikTokConnection.kind),
                col(BCConnectionBinding.status) == "ACTIVE",
            ),
        )
        .join(
            ConnectionAuthorization,
            and_(
                col(ConnectionAuthorization.tenant_id) == grants.c.tenant_id,
                col(ConnectionAuthorization.connection_id) == grants.c.connection_id,
                col(ConnectionAuthorization.authorization_revision)
                == col(TikTokConnection.authorization_revision),
            ),
        )
        .where(
            grants.c.checked_at <= now,
            col(ConnectionAuthorization.verified_at) <= now,
            ConnectionAuthorization.source != "",
            ConnectionAuthorization.source != "UNKNOWN",
            col(ConnectionAuthorization.permission_summary)["read_authorized"]
            == func.to_jsonb(True),
        )
        .cte("readable_history_accounts")
    )
    # FrozenTikTokRoute 的 binding_revision 默认值为 0；其它字段必须完整匹配。
    route = (
        func.jsonb_build_object("binding_revision", 0)
        .cast(JSONB)
        .op("||")(col(ManagementTask.route))
    )
    account_visible = (
        select(readable.c.advertiser_id)
        .where(
            readable.c.route == route,
            readable.c.advertiser_id
            == col(ManagementTaskItem.ref)["advertiser_id"].astext,
            func.jsonb_typeof(col(ManagementTaskItem.ref)["advertiser_id"]) == "string",
            func.trim(readable.c.advertiser_id) != "",
        )
        .correlate(ManagementTask, ManagementTaskItem)
        .exists()
    )
    hidden_item = (
        select(ManagementTaskItem.id)
        .where(
            ManagementTaskItem.tenant_id == context.tenant_id,
            col(ManagementTaskItem.task_id) == col(ManagementTask.id),
            ~account_visible,
        )
        .correlate(ManagementTask)
        .exists()
    )
    # 准备前尚无 task items；冻结账户也必须逐集合满足原连接与代数授权。
    frozen_accounts = (
        func.jsonb_array_elements_text(
            func.coalesce(
                ManagementTask.request["selection"]["advertiser_ids"], cast([], JSONB)
            )
        )
        .table_valued("value")
        .render_derived(name="frozen_accounts")
    )
    hidden_frozen = (
        select(frozen_accounts.c.value)
        .where(
            ~select(readable.c.advertiser_id)
            .where(
                readable.c.route == route,
                readable.c.advertiser_id == frozen_accounts.c.value,
            )
            .correlate(ManagementTask, frozen_accounts)
            .exists()
        )
        .correlate(ManagementTask)
        .exists()
    )
    return and_(
        select(readable.c.advertiser_id)
        .where(readable.c.route == route)
        .correlate(ManagementTask)
        .exists(),
        ~hidden_item,
        ~hidden_frozen,
    )
