"""只读未知回执核查任务；数据库暂时不可用时由broker退避重试。"""

from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.ad_management.reconciliation import reconcile_unknown_items

RECONCILIATION_TASK = "ad_management.reconcile"
register_dispatch_task(RECONCILIATION_TASK, "ad-management")


@celery_app.task(
    name=RECONCILIATION_TASK,
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=None,
)
def reconcile_management_task(*, tenant_id: str, actor_id: str, payload: dict):
    validate_dispatch_payload(payload)
    try:
        if set(payload) != {"task_id", "item_ids", "attempt", "round"}:
            raise ValueError("invalid keys")
        tenant, actor, task = UUID(tenant_id), UUID(actor_id), UUID(payload["task_id"])
        if (
            type(payload["item_ids"]) is not list
            or not 1 <= len(payload["item_ids"]) <= 20
        ):
            raise ValueError("invalid items")
        items = tuple(UUID(item) for item in payload["item_ids"])
        attempt, round_key = payload["attempt"], payload["round"]
        if (
            type(attempt) is not int
            or not 1 <= attempt <= 3
            or len(set(items)) != len(items)
        ):
            raise ValueError("invalid attempt")
        if round_key != "auto":
            UUID(round_key)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "核查任务参数无效") from exc
    reconcile_unknown_items(
        engine, tenant, actor, task, items, attempt, round_key=round_key
    )
