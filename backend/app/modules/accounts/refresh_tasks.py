"""自动刷新只调度内部 attempt 引用，不让队列承载凭据。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis import Redis

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.mcp_auth.refresh import (
    _locked,
    _queue_refresh,
    _require_actor,
    process_mcp_refresh,
)
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.modules.tenants.permissions import require_tenant


@celery_app.task(name="accounts.refresh_mcp", time_limit=45, soft_time_limit=40)  # type: ignore[untyped-decorator]
def refresh_mcp(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    validate_dispatch_payload(payload)
    if set(payload) != {"attempt_id"}:
        raise DomainError("dispatch_payload_invalid", "刷新任务参数无效")
    try:
        tenant, actor, attempt_id = (
            UUID(tenant_id),
            UUID(actor_id),
            UUID(payload["attempt_id"]),
        )
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "刷新任务参数无效") from None
    with bounded_session(
        engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
    ) as session:
        require_tenant(session, actor_id=actor, tenant_id=tenant, action="read")
        context = TenantContext(tenant_id=tenant, actor_id=actor, role="operator")
        _, attempt = _locked(session, attempt_id)
        _require_actor(session, context=context, attempt=attempt)
        if (
            attempt.status in ("PENDING", "CLAIMED", "REQUEST_ARMED", "CANDIDATE_READY")
            and not attempt.error_code
        ):
            _queue_refresh(
                session,
                context=context,
                attempt=attempt,
                due=datetime.now(UTC) + timedelta(seconds=50),
            )
            session.commit()
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        return process_mcp_refresh(
            database_engine=engine,
            redis_client=redis_client,
            context=context,
            attempt_id=attempt_id,
        )
