"""Celery 入口：每次推进一个广告目录页，目录完成后才发布。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import DirectoryQuery
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.routing import verify_route
from app.modules.ads.sync import publish_directory, stage_directory_page
from app.modules.ads.sync_models import AdDirectoryPage, AdDirectoryRun

TASK_NAME = "ads.sync_step"
register_dispatch_task(TASK_NAME, "ads-directory")
PUBLISH_TASK_NAME = "ads.publish_step"
register_dispatch_task(PUBLISH_TASK_NAME, "ads-publish")

_LEASE_SECONDS = 90
_LOCK_TIMEOUT = "20s"
_TRANSACTION_TIMEOUT = "45s"


@contextmanager
def _directory_session(database_engine: Any) -> Iterator[Session]:
    """领取和发布均使用有界事务，异常时丢弃物理连接以释放服务器行锁。"""
    with Session(database_engine) as session:
        try:
            session.connection().execute(
                text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
            )
            session.connection().execute(text("SET LOCAL statement_timeout = '30s'"))
            # 多条短语句也有总事务预算，早于 Celery 的 55 秒信号退出。
            session.connection().execute(
                text(f"SET LOCAL transaction_timeout = '{_TRANSACTION_TIMEOUT}'")
            )
            # 发布没有外部请求；即使进程被软超时中断且回滚失败，也由服务器兜底。
            session.connection().execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            )
            yield session
        except BaseException:
            # psycopg 被 Celery 信号打断后可能仍有未完成命令，普通 close/rollback
            # 会报 another command is already in progress，必须直接关闭物理连接。
            session.invalidate()
            raise


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
    publish_only: bool = False,
) -> str:
    # 实例总开关同时拦截已经排队的目录任务；重新开启后由扫描器恢复。
    if not settings.ADS_SYNC_ENABLED:
        return "WAIT"
    with _directory_session(database_engine) as session:
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
        now = datetime.now(UTC)
        # A crashed worker leaves a short lease.  A live duplicate must not
        # open a second provider call; the scanner advances generation only
        # after this lease expires.
        if run.claimed_until is not None and run.claimed_until > now:
            return "WAIT"
        run.claim_token = uuid4()
        run.claimed_until = now + timedelta(seconds=_LEASE_SECONDS)
        route = _route(run.frozen_route)
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=run.advertiser_id,
            capability="read",
        )
        terminal = session.exec(
            select(AdDirectoryPage.page)
            .where(
                AdDirectoryPage.run_id == run_id,
                col(AdDirectoryPage.complete).is_(True),
            )
            .limit(1)
        ).first()
        if terminal is not None:
            # 末页已持久化：独占一次任务期限做本地发布，恢复时也不再读远端。
            publish_directory(session, run_id=run_id, claim_generation=claim_generation)
            run.claim_token = run.claimed_until = None
            session.commit()
            return "READY"
        if publish_only:
            raise DomainError("directory_incomplete", "目录发布缺少已暂存末页")
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
    with _directory_session(database_engine) as session:
        stage_directory_page(
            session,
            run_id=run_id,
            page=page,
            claim_generation=claim_generation,
        )
        current = session.get(AdDirectoryRun, run_id)
        if current is None or (not page.complete and page.next_page is None):
            raise DomainError("directory_incomplete", "目录页缺少后续游标")
        if current.claim_generation != claim_generation:
            # 网络请求期间可能已经换代；相同旧页可复用，但不能清掉新 worker 的租约。
            return "FAILED"
        # 末页之后的游标只标识发布续步；领取时先识别暂存末页，不会请求该页。
        next_page = page.page + 1 if page.complete else page.next_page
        assert next_page is not None  # 上方已经拒绝非末页缺失游标。
        current.next_page = next_page
        current.status = "RUNNING"
        current.next_attempt_at = datetime.now(UTC)
        current.claim_token = current.claimed_until = None
        # 续步与页同事务提交，统一扫描器的幂等键，消息丢失仍可换代恢复。
        enqueue_after_commit(
            session,
            context=context,
            task_name=PUBLISH_TASK_NAME if page.complete else TASK_NAME,
            task_key=f"ads.sync_step:{run_id}:{claim_generation}:page:{current.next_page}",
            payload={"run_id": str(run_id), "claim_generation": claim_generation},
        )
        session.commit()
        return "CONTINUE"


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


@celery_app.task(  # type: ignore[untyped-decorator]
    name=PUBLISH_TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def publish_step(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    run_id, generation = _payload(payload)
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        return run_directory_step(
            database_engine=engine,
            redis_client=redis_client,
            context=_context(tenant_id, actor_id),
            run_id=run_id,
            claim_generation=generation,
            publish_only=True,
        )


__all__ = [
    "TASK_NAME",
    "PUBLISH_TASK_NAME",
    "sync_step",
    "publish_step",
    "run_directory_step",
]
