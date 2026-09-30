"""Celery 入口：每次推进一个广告目录页，目录完成后才发布。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis import Redis
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryQuery
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryRun

TASK_NAME = "ads.sync_step"
register_dispatch_task(TASK_NAME, "resources")


def _context(tenant_id: str, actor_id: str) -> TenantContext:
    try:
        return TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "目录任务操作者无效") from exc


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    validate_dispatch_payload(payload)
    if set(payload) != {"run_id", "claim_generation"}:
        raise DomainError("dispatch_payload_invalid", "目录任务参数无效")
    try:
        run_id = UUID(str(payload["run_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "目录运行标识无效") from exc
    generation = payload["claim_generation"]
    if type(generation) is not int or generation < 1:
        raise DomainError("dispatch_payload_invalid", "目录领取代数无效")
    return run_id, generation


def run_directory_step(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    claim_generation: int,
) -> str:
    with Session(database_engine) as session:
        run = session.exec(
            select(AdDirectoryRun)
            .where(
                AdDirectoryRun.id == run_id,
                AdDirectoryRun.tenant_id == context.tenant_id,
            )
            .with_for_update()
        ).one_or_none()
        if run is None:
            raise DomainError("directory_run_not_found", "目录同步运行不存在")
        if run.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "目录任务操作者不匹配")
        if run.claim_generation != claim_generation:
            return "FAILED"
        if (
            run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}
            or run.published_version is not None
        ):
            return "FAILED" if run.status == "FAILED" else "READY"
        route = _route(run.frozen_route)
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=run.advertiser_id,
            capability="read",
        )
        query_data = dict(run.query)
        # Celery retries must resume the persisted cursor.  The durable query is
        # the contract; page is the only mutable transport cursor.
        query_data["page"] = run.next_page
        query_data["ids"] = tuple(query_data.get("ids", ()))
        query_data["parent_ids"] = tuple(query_data.get("parent_ids", ()))
        query = DirectoryQuery(**query_data)
        session.commit()

    deadline = datetime.now(UTC) + timedelta(seconds=45)
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        page = gateway.ads.read_page(query)
    with Session(database_engine) as session:
        stage_directory_page(
            session,
            run_id=run_id,
            page=page,
            claim_generation=claim_generation,
        )
        if page.complete:
            publish_directory(session, run_id=run_id, claim_generation=claim_generation)
            result = "READY"
        else:
            current = session.get(AdDirectoryRun, run_id)
            if current is None or page.next_page is None:
                raise DomainError("directory_incomplete", "目录页缺少后续游标")
            current.next_page = page.next_page
            current.status = "RUNNING"
            result = "CONTINUE"
        session.commit()
        return result


def _route(value: dict[str, Any]):
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

    try:
        return FrozenTikTokRoute.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_changed", "目录任务冻结路由无效") from exc


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_step(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    run_id, generation = _payload(payload)
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        return run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=_context(tenant_id, actor_id),
            run_id=run_id,
            claim_generation=generation,
        )


__all__ = ["TASK_NAME", "sync_step", "run_directory_step"]
