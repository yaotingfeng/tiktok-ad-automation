"""租户公平只排序；每个物理 HTTP 的共享额度由 gateway 独占申请。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.admission import quota_scope
from app.integrations.tiktok.bounded_resources import bounded_redis
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.builds.fairness import finish_fair_turn, take_fair_turn

HARD_LIMIT = 45


@contextmanager
def admitted_build_call(
    redis_client: Any,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    task_deadline: datetime,
) -> Iterator[None]:
    if context.tenant_id != route.tenant_id:
        raise DomainError("action_forbidden", "公平调度租户不匹配")
    scope = quota_scope(
        channel=route.channel,
        app_id=settings.TIKTOK_APP_ID if route.channel == "OFFICIAL_API" else None,
        verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
    )
    owner = uuid4()
    with bounded_redis(redis_client, task_deadline=task_deadline) as bounded:
        if not take_fair_turn(
            bounded,
            app_scope=scope,
            tenant_id=context.tenant_id,
            owner=owner,
            wait_ms=30000,
            turn_ms=3000,
        ):
            raise AccountAdmissionDeferred(1000)
        # 公平轮次在进入任何会话 HTTP 前结束；gateway 的准入拒绝另行持久重排。
        # 此层既不消费额度也不释放额度，避免两个桶重复计数或清理提前放行。
        finish_fair_turn(
            bounded,
            app_scope=scope,
            tenant_id=context.tenant_id,
            owner=owner,
            served=True,
        )
    yield
