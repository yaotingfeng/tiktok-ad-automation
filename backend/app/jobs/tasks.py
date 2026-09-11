"""Known dispatch tasks. Registration is an application startup operation.

P02+ modules register_dispatch_task(name, queue) and define their Celery handler
with keyword arguments tenant_id, actor_id, payload. Every business handler must
parse its own payload and re-query current permissions using P02 require_tenant;
a role from a request or queue message never constitutes authorization.
"""

from time import monotonic
from uuid import UUID

from app.core.config import settings
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

_ALLOWED_QUEUES = frozenset({"control", "resources", "builds"})
_DISPATCH_TASKS: dict[str, str] = {}


def register_dispatch_task(name: str, queue: str) -> None:
    """Register an explicit task-to-queue mapping; conflicting mappings fail."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name == "jobs.flush_dispatch"
    ):
        raise ValueError("Invalid business dispatch task name")
    if queue not in _ALLOWED_QUEUES:
        raise ValueError("Unknown dispatch queue")
    if name in _DISPATCH_TASKS and _DISPATCH_TASKS[name] != queue:
        raise ValueError("Dispatch task already registered with another queue")
    _DISPATCH_TASKS[name] = queue


def dispatch_queue(name: str) -> str:
    try:
        return _DISPATCH_TASKS[name]
    except KeyError, TypeError:
        raise DomainError("dispatch_task_not_allowed", "任务未注册") from None


register_dispatch_task("jobs.probe", "control")
register_dispatch_task("accounts.refresh_mcp", "control")


@celery_app.task(name="jobs.probe")
def probe(*, tenant_id: str, actor_id: str, payload: dict) -> None:
    """Internal no-op diagnostics only; does not grant or verify business access."""
    UUID(tenant_id)
    UUID(actor_id)
    from app.jobs.outbox import validate_dispatch_payload

    validate_dispatch_payload(payload)


def drain_dispatch(limit: int = 100) -> int:
    """Drain bounded fair rounds, committing each before considering another.

    A time budget is checked between complete rounds, never inside a transaction.
    No progress ends this delivery; broker backoff remains with its original row.
    These are local queue messages, not permissions to make external API calls.
    """
    from app.jobs.outbox import flush_dispatch

    deadline = monotonic() + settings.DISPATCH_TIME_BUDGET_SECONDS
    published = 0
    for _ in range(settings.DISPATCH_MAX_ROUNDS):
        count = flush_dispatch(limit=limit)
        published += count
        if count == 0 or monotonic() >= deadline:
            break
    return published


@celery_app.task(name="jobs.flush_dispatch", time_limit=30, soft_time_limit=25)
def flush_dispatch_task(limit: int = 100) -> int:
    return drain_dispatch(limit=limit)
