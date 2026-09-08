"""Production process deadlines cover S3, DNS, SDK buffering and response reads."""

from typing import Any
from uuid import UUID

from billiard.process import current_process  # type: ignore[import-untyped]
from redis import Redis

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.tasks import register_dispatch_task

from .source_uploads import READ_HARD_LIMIT, UPLOAD_HARD_LIMIT, run_source_upload

register_dispatch_task("materials.upload_original", "resources")
register_dispatch_task("materials.verify_original", "resources")


def _run(
    task: Any,
    *,
    kind: str,
    hard_limit: int,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
) -> None:
    limits = task.request.timelimit or (None, None)
    effective = limits[0] or task.time_limit
    if (
        not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or task.request.called_directly
        or task.request.is_eager
        or type(effective) not in (int, float)
        or not 0 < effective <= hard_limit
    ):
        raise DomainError(
            "material_worker_unbounded", "素材任务需要启用 prefork 硬超时工作进程"
        )
    if (
        set(payload) - {"material_id", "operation_id", "claim_id", "revision"}
        or "material_id" not in payload
    ):
        raise DomainError("invalid_asset_task", "素材工作任务参数无效")
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        run_source_upload(
            database_engine=engine,
            redis_client=redis_client,
            context=TenantContext(
                tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
            ),
            material_id=UUID(payload["material_id"]),
            kind=kind,
            operation_id=UUID(payload["operation_id"])
            if payload.get("operation_id")
            else None,
            recovery_claim_id=UUID(payload["claim_id"])
            if payload.get("claim_id")
            else None,
            revision=payload.get("revision"),
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.upload_original",
    bind=True,
    time_limit=UPLOAD_HARD_LIMIT,
    soft_time_limit=UPLOAD_HARD_LIMIT - 10,
)
def upload_original(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run(
        self,
        kind="upload",
        hard_limit=UPLOAD_HARD_LIMIT,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.verify_original",
    bind=True,
    time_limit=READ_HARD_LIMIT,
    soft_time_limit=READ_HARD_LIMIT - 5,
)
def verify_original(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run(
        self,
        kind="verify",
        hard_limit=READ_HARD_LIMIT,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
    )
