"""Celery 入口：每次只推进一个已持久化的报表分片。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.ads.balances import persist_balance
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.contracts import decode_query
from app.modules.reporting.scheduling import (
    enqueue_due_syncs,
    record_schedule_completion,
)
from app.modules.reporting.sync import collect_report_step
from app.modules.reporting.sync_models import ReportSyncRun, SyncSchedule

TASK_NAME = "reporting.sync_step"
SCAN_TASK_NAME = "reporting.scan_due"
register_dispatch_task(TASK_NAME, "ads-reporting")
register_dispatch_task(SCAN_TASK_NAME, "control")

_LEASE_SECONDS = 90


def _successor_key(run: ReportSyncRun, *, claim_generation: int) -> str:
    """Stable key shared by worker continuation and Beat recovery."""

    state = (
        f"page:{run.next_page}"
        f":task:{run.task_id or '-'}:status:{run.task_status or '-'}"
        f":due:{run.next_attempt_at.isoformat() if run.next_attempt_at else '-'}"
    )
    return f"{TASK_NAME}:{run.id}:{claim_generation}:{sha256(state.encode()).hexdigest()}"


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
        if run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"} or run.published_version is not None:
            return "FAILED" if run.status == "FAILED" else "READY"
        now = datetime.now(UTC)
        # 旧 broker 消息或重复投递也必须服从持久退避；到期前不领取、
        # 不建立 gateway，不能只依赖 Outbox 的投递时间防止紧循环轮询。
        if run.next_attempt_at is not None and run.next_attempt_at > now:
            return "WAIT"
        route = _route(run.frozen_route)
        task_status = run.task_status
        advertiser_id = run.advertiser_id
        persisted_ad_type = run.query.get("ad_type")
        if persisted_ad_type is not None and (
            not isinstance(persisted_ad_type, str) or not persisted_ad_type.strip()
        ):
            raise DomainError("report_query_invalid", "报表广告类型无效")
        ad_type = persisted_ad_type.strip().upper() if persisted_ad_type else None
        if run.claimed_until is not None and run.claimed_until > now:
            return "WAIT"
        run.claim_token = uuid4()
        run.claimed_until = now + timedelta(seconds=_LEASE_SECONDS)
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
        ad_type=ad_type,
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
                current.claim_token = None
                current.claimed_until = None
                session.commit()
                return "READY"
        with Session(database_engine) as session:
            result = collect_report_step(
                session,
                run_id=run_id,
                claim_generation=claim_generation,
                gateway=gateway,
            )
            current = session.get(ReportSyncRun, run_id, populate_existing=True)
            if current is not None and current.claim_generation == claim_generation:
                current.claim_token = None
                current.claimed_until = None
                if result in {"CONTINUE", "WAIT"}:
                    current.next_attempt_at = current.next_attempt_at or datetime.now(UTC)
            if (
                result in {"CONTINUE", "WAIT"}
                and current is not None
                and current.claim_generation == claim_generation
            ):
                dispatch_id = enqueue_after_commit(
                    session,
                    context=context,
                    task_name=TASK_NAME,
                    task_key=_successor_key(current, claim_generation=claim_generation),
                    payload={"run_id": str(run_id), "claim_generation": claim_generation},
                )
                dispatch = session.get(PendingDispatch, dispatch_id)
                if dispatch is not None and current.next_attempt_at is not None:
                    # Outbox eligibility is part of the same transaction as
                    # WAIT state; a poll can never run before its durable due.
                    dispatch.available_at = max(
                        dispatch.available_at, current.next_attempt_at
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


def scan_due_runs(*, database_engine: Any, now: datetime | None = None) -> int:
    """Claim due plans and enqueue bounded work without contacting TikTok.

    A lease-expired run gets a new generation before its successor is written;
    any old worker that returns later is fenced by the generation check in the
    directory/report collector.  All state and outbox rows commit together.
    """

    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    queued = 0
    with Session(database_engine) as session, session.begin():
        request_ids = enqueue_due_syncs(session, now=now)
        # Include every shard of each request, not just the first ID returned by
        # request_sync; report requests commonly fan out into several contracts.
        # 与 worker 领取使用同一行锁；跳过活跃事务，避免扫描旧快照后覆盖
        # 另一会话刚提交的 lease，或两个 scanner 同时恢复同一代数。
        directory_rows = session.exec(
            select(AdDirectoryRun).where(
                col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
                col(AdDirectoryRun.next_attempt_at).is_(None)
                | (col(AdDirectoryRun.next_attempt_at) <= now),
            ).with_for_update(skip_locked=True)
        ).all()
        report_rows = session.exec(
            select(ReportSyncRun).where(
                col(ReportSyncRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
                col(ReportSyncRun.next_attempt_at).is_(None)
                | (col(ReportSyncRun.next_attempt_at) <= now),
            ).with_for_update(skip_locked=True)
        ).all()
        # Short current-day shards are served before the long historical
        # backfill.  The two queues still have independent one-slot consumers,
        # so a large history cannot consume the control/other worker pool.
        def priority(row: ReportSyncRun) -> tuple[int, UUID]:
            query = row.query if isinstance(row.query, dict) else {}
            try:
                start = datetime.fromisoformat(str(query["start_date"]))
                end = datetime.fromisoformat(str(query["end_date"]))
                span = (end - start).days
            except (KeyError, TypeError, ValueError):
                span = 10_000
            return (span, row.id)

        report_rows = sorted(report_rows, key=priority)
        del request_ids  # selection above intentionally also recovers after restart
        for row, task_name in [
            *[(item, "ads.sync_step") for item in directory_rows],
            *[(item, TASK_NAME) for item in report_rows],
        ]:
            if row.claimed_until is not None and row.claimed_until > now:
                continue
            if row.claimed_until is not None and row.claimed_until <= now:
                row.claim_generation += 1
                row.claim_token = None
                row.claimed_until = None
            context = TenantContext(tenant_id=row.tenant_id, actor_id=row.actor_id, role="operator")
            task_key = (
                _successor_key(row, claim_generation=row.claim_generation)
                if isinstance(row, ReportSyncRun)
                else f"{task_name}:{row.id}:{row.claim_generation}:page:{row.next_page}:task:{getattr(row, 'task_id', None) or '-'}"
            )
            enqueue_after_commit(
                session,
                context=context,
                task_name=task_name,
                task_key=task_key,
                payload={"run_id": str(row.id), "claim_generation": row.claim_generation},
            )
            queued += 1
    return queued


@celery_app.task(name=SCAN_TASK_NAME, time_limit=30, soft_time_limit=25)
def scan_due(
    *,
    tenant_id: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    # Beat supplies no tenant; the keyword shape remains compatible with the
    # standard outbox task contract for controlled/manual invocations.
    del tenant_id, actor_id, payload
    return scan_due_runs(database_engine=engine)


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


__all__ = ["TASK_NAME", "SCAN_TASK_NAME", "sync_step", "scan_due", "run_report_step", "scan_due_runs"]
