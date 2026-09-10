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
register_dispatch_task("materials.prepare_target", "resources")
register_dispatch_task("materials.verify_target", "resources")


def require_bounded_worker(task: Any, *, hard_limit: int) -> None:
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


def _run(
    task: Any,
    *,
    kind: str,
    hard_limit: int,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
) -> None:
    require_bounded_worker(task, hard_limit=hard_limit)
    if (
        set(payload)
        - {
            "material_id",
            "operation_id",
            "claim_id",
            "revision",
            "object_id",
            "generation",
        }
        or "material_id" not in payload
    ):
        raise DomainError("invalid_asset_task", "素材工作任务参数无效")
    if ("object_id" in payload) != ("generation" in payload) or (
        "generation" in payload
        and (type(payload["generation"]) is not int or payload["generation"] < 1)
    ):
        raise DomainError("invalid_asset_task", "原件任务缺少精确代次")
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
            object_id=UUID(payload["object_id"]) if payload.get("object_id") else None,
            generation=payload.get("generation"),
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


def _run_target(
    task: Any,
    *,
    kind: str,
    hard_limit: int,
    tenant_id: str,
    actor_id: str,
    payload: dict[str, Any],
) -> None:
    require_bounded_worker(task, hard_limit=hard_limit)
    if set(payload) - {
        "distribution_id",
        "operation_id",
        "revision",
        "claim_id",
        "observe",
        "read_only",
    } or not {"distribution_id", "operation_id"}.issubset(payload):
        raise DomainError("invalid_asset_task", "目标素材工作任务参数无效")
    if "read_only" in payload and type(payload["read_only"]) is not bool:
        raise DomainError("invalid_asset_task", "素材只读标记无效")
    from .distribution import run_distribution

    with Redis.from_url(settings.REDIS_URL) as redis_client:
        run_distribution(
            database_engine=engine,
            redis_client=redis_client,
            context=TenantContext(
                tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
            ),
            distribution_id=UUID(payload["distribution_id"]),
            operation_id=UUID(payload["operation_id"]),
            kind=kind,
            read_only=payload.get("read_only", False),
            revision=payload.get("revision"),
            recovery_claim_id=UUID(payload["claim_id"])
            if payload.get("claim_id")
            else None,
        )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.prepare_target",
    bind=True,
    time_limit=UPLOAD_HARD_LIMIT,
    soft_time_limit=UPLOAD_HARD_LIMIT - 10,
)
def prepare_target(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run_target(
        self,
        kind="prepare",
        hard_limit=UPLOAD_HARD_LIMIT,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="materials.verify_target",
    bind=True,
    time_limit=READ_HARD_LIMIT,
    soft_time_limit=READ_HARD_LIMIT - 5,
)
def verify_target(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    _run_target(
        self,
        kind="verify",
        hard_limit=READ_HARD_LIMIT,
        tenant_id=tenant_id,
        actor_id=actor_id,
        payload=payload,
    )


@celery_app.task(name="materials.repair_dispatches", time_limit=45)  # type: ignore[untyped-decorator]
def repair_dispatches(limit: int = 100) -> int:
    from sqlmodel import Session

    from .distribution import repair_material_dispatches

    with Session(engine) as session, session.begin():
        return repair_material_dispatches(session, limit=limit)
