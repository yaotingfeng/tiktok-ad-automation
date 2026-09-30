"""Celery 入口：每次只推进一个已持久化的报表分片。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis import Redis
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.celery_app import celery_app
from app.jobs.outbox import validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.ads.balances import persist_balance
from app.modules.reporting.contracts import decode_query
from app.modules.reporting.scheduling import record_schedule_completion
from app.modules.reporting.sync import collect_report_step
from app.modules.reporting.sync_models import ReportSyncRun, SyncSchedule

TASK_NAME = "reporting.sync_step"
register_dispatch_task(TASK_NAME, "resources")


def _context(tenant_id: str, actor_id: str) -> TenantContext:
    try:
        return TenantContext(
            tenant_id=UUID(tenant_id), actor_id=UUID(actor_id), role="operator"
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "报表任务操作者无效") from exc


def _payload(payload: dict[str, Any]) -> tuple[UUID, int]:
    validate_dispatch_payload(payload)
    if set(payload) != {"run_id", "claim_generation"}:
        raise DomainError("dispatch_payload_invalid", "报表任务参数无效")
    try:
        run_id = UUID(str(payload["run_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("dispatch_payload_invalid", "报表运行标识无效") from exc
    generation = payload["claim_generation"]
    if type(generation) is not int or generation < 1:
        raise DomainError("dispatch_payload_invalid", "报表领取代数无效")
    return run_id, generation


def run_report_step(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    run_id: UUID,
    claim_generation: int,
) -> str:
    """Re-read route/actor, perform one bounded gateway call, then commit progress."""

    with Session(database_engine) as session:
        run = session.exec(
            select(ReportSyncRun)
            .where(
                ReportSyncRun.id == run_id, ReportSyncRun.tenant_id == context.tenant_id
            )
            .with_for_update()
        ).one_or_none()
        if run is None:
            raise DomainError("report_run_not_found", "报表同步运行不存在")
        if run.actor_id != context.actor_id:
            raise DomainError("action_forbidden", "报表任务操作者不匹配")
        if run.claim_generation != claim_generation:
            return "FAILED"
        route = _route(run.frozen_route)
        task_status = run.task_status
        advertiser_id = run.advertiser_id
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=run.advertiser_id,
            capability="read",
        )
        session.commit()

    deadline = datetime.now(UTC) + timedelta(seconds=45)
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=deadline,
    ) as gateway:
        if task_status == "BALANCE":
            balance = gateway.ads.read_balance(advertiser_id)
            with Session(database_engine) as session:
                current = session.exec(
                    select(ReportSyncRun)
                    .where(
                        ReportSyncRun.id == run_id,
                        ReportSyncRun.tenant_id == context.tenant_id,
                    )
                    .with_for_update()
                ).one_or_none()
                if current is None or current.claim_generation != claim_generation:
                    return "FAILED"
                persist_balance(
                    session,
                    context=context,
                    route=route,
                    advertiser_id=current.advertiser_id,
                    balance=balance,
                )
                current.status = "COMPLETE"
                current.coverage = "COMPLETE"
                current.completed_at = datetime.now(UTC)
                current.observed_at = datetime.now(UTC)
                session.commit()
                return "READY"
        with Session(database_engine) as session:
            result = collect_report_step(
                session,
                run_id=run_id,
                claim_generation=claim_generation,
                gateway=gateway,
            )
            if result == "READY":
                _record_request_completion(session, run_id=run_id)
            session.commit()
            return result


def _record_request_completion(session: Session, *, run_id: UUID) -> None:
    """Mark schedule coverage only after every report shard is published."""

    current = session.get(ReportSyncRun, run_id, populate_existing=True)
    if current is None or current.task_status == "BALANCE":
        return
    runs = session.exec(
        select(ReportSyncRun).where(
            ReportSyncRun.tenant_id == current.tenant_id,
            ReportSyncRun.request_id == current.request_id,
        )
    ).all()
    if not runs or any(row.status != "COMPLETE" for row in runs):
        return
    dates = []
    for row in runs:
        try:
            query = decode_query(row.query)
        except TypeError, ValueError, KeyError:
            continue
        dates.append((query.start_date, query.end_date))
    if not dates:
        return
    start_date = min(item[0] for item in dates)
    end_date = max(item[1] for item in dates)
    schedules = session.exec(
        select(SyncSchedule).where(
            SyncSchedule.tenant_id == current.tenant_id,
            col(SyncSchedule.last_request_id).in_([row.id for row in runs]),
        )
    ).all()
    for schedule in schedules:
        record_schedule_completion(
            session,
            schedule_id=schedule.id,
            start_date=start_date,
            end_date=end_date,
        )


def _route(value: dict[str, Any]):
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

    try:
        return FrozenTikTokRoute.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("frozen_route_changed", "报表任务冻结路由无效") from exc


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
        return run_report_step(
            database_engine=engine,
            redis_client=redis_client,
            context=_context(tenant_id, actor_id),
            run_id=run_id,
            claim_generation=generation,
        )


__all__ = ["TASK_NAME", "sync_step", "run_report_step"]
