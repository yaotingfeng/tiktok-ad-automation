"""同冻结路由的有界目录再观察派发；每步落一页后关闭事务。"""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.mcp_discovery_tasks import (
    _detail_ids,
    advance_discovery_stage,
    read_discovery_stage,
    stage_results,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.accounts.runtime_directory import (
    MODE,
    TASK_NAME,
    locked_runtime_run,
    publish_runtime_directory,
    queue_runtime,
)


def _failure(
    database_engine: Any,
    *,
    context: TenantContext,
    run_id: UUID,
    revision: int,
    claim_id: UUID | None,
    error: Exception,
) -> None:
    code = (
        error.code
        if isinstance(error, DomainError)
        else "discovery_stale"
        if isinstance(error, IntegrityError)
        else "discovery_failed"
    )
    with bounded_session(
        database_engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
    ) as session:
        identity = session.get(DiscoveryRun, run_id)
        if (
            identity is None
            or (identity.tenant_id, identity.actor_id)
            != (context.tenant_id, context.actor_id)
            or identity.work.get("mode") != MODE
        ):
            return
        session.exec(
            select(TikTokConnection)
            .where(TikTokConnection.id == identity.connection_id)
            .with_for_update()
        ).one()
        run = session.exec(
            select(DiscoveryRun)
            .where(DiscoveryRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if (
            run.revision != revision
            or run.status not in {"RUNNING", "ADMISSION_WAIT"}
            or (
                run.claim_id != claim_id
                and not (
                    claim_id is None
                    and run.claimed_until is not None
                    and run.claimed_until <= datetime.now(UTC)
                )
            )
        ):
            return
        run.claim_id = run.claimed_until = None
        run.error_code = code
        if isinstance(error, AccountAdmissionDeferred) or code in {
            "admission_unavailable",
            "tiktok_local_resources_unavailable",
            "gateway_credentials_changed",
            "mcp_refresh_pending",
        }:
            run.status = "ADMISSION_WAIT"
            run.next_attempt_at = datetime.now(UTC) + timedelta(
                milliseconds=max(
                    1,
                    error.retry_after_ms
                    if isinstance(error, AccountAdmissionDeferred)
                    else 5000,
                )
            )
            run.revision += 1
            queue_runtime(session, run, due=run.next_attempt_at)
        else:
            run.status = "ERROR"
            try:
                job_id = UUID(run.work["capability_job_id"])
            except KeyError, ValueError, TypeError:
                job_id = None
            job = session.get(CapabilityJob, job_id) if job_id else None
            if (
                job is not None
                and (job.tenant_id, job.actor_id, job.connection_id)
                == (run.tenant_id, run.actor_id, run.connection_id)
                and job.status == "PENDING"
            ):
                job.status = (
                    "STALE"
                    if code
                    in {
                        "route_authorization_changed",
                        "route_contract_changed",
                        "discovery_stale",
                    }
                    else "BLOCKED"
                    if code
                    in {
                        "action_forbidden",
                        "tenant_forbidden",
                        "connection_unavailable",
                        "mcp_refresh_unknown",
                        "mcp_refresh_reauth_required",
                    }
                    else "FAILED"
                )
                job.error_code = code
                job.claim_token = job.claimed_until = None
                session.add(job)
        session.add(run)
        session.commit()


def process_runtime_directory(
    *,
    database_engine: Any,
    redis_client: Redis,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    try:
        if (
            set(payload) != {"run_id", "revision"}
            or type(payload["run_id"]) is not str
            or type(payload["revision"]) is not int
            or payload["revision"] < 0
        ):
            raise ValueError("invalid payload")
        run_id, revision = UUID(payload["run_id"]), payload["revision"]
    except KeyError, ValueError, TypeError:
        raise DomainError("dispatch_payload_invalid", "运行目录任务参数无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    claim_id = uuid4()
    claimed = False
    try:
        with bounded_session(database_engine, task_deadline=deadline) as session:
            identity = session.get(DiscoveryRun, run_id)
            if (
                identity is None
                or identity.tenant_id != tenant_id
                or identity.actor_id != actor_id
            ):
                return
            if (
                identity.status not in {"RUNNING", "ADMISSION_WAIT"}
                or identity.revision != revision
            ):
                return
            now = datetime.now(UTC)
            if (identity.claimed_until and identity.claimed_until > now) or (
                identity.next_attempt_at and identity.next_attempt_at > now
            ):
                return
            run, _, route = locked_runtime_run(
                session, context=context, run_id=run_id, revision=revision
            )
            # 首次无锁读取只用于快速去重；连接/run锁后必须再检查，避免双worker抢同页。
            now = datetime.now(UTC)
            if (run.claimed_until and run.claimed_until > now) or (
                run.next_attempt_at and run.next_attempt_at > now
            ):
                return
            work = copy.deepcopy(run.work)
            requested_ids = (
                _detail_ids(session, run=run) if work["stage"] == "DETAILS" else ()
            )
            if work["stage"] != "FINALIZE":
                run.sent_count += 1
            run.claim_id = claim_id
            run.claimed_until = now + timedelta(seconds=60)
            session.add(run)
            queue_runtime(
                session, run, suffix=f"recover:{claim_id}", due=run.claimed_until
            )
            session.commit()
            claimed = True

        def before_request() -> None:
            # 初始化、协议与每个业务HTTP都重新核验当前build权限及原claim，事务不跨HTTP。
            with bounded_session(database_engine, task_deadline=deadline) as session:
                locked_runtime_run(
                    session,
                    context=context,
                    run_id=run_id,
                    claim_id=claim_id,
                    revision=revision,
                )

        if work["stage"] == "FINALIZE":
            with bounded_session(database_engine, task_deadline=deadline) as session:
                conflict = False
                try:
                    publish_runtime_directory(
                        session,
                        context=context,
                        run_id=run_id,
                        claim_id=claim_id,
                        revision=revision,
                    )
                    session.commit()
                except IntegrityError:
                    # 业务唯一冲突不能被资源包装器误判为可重试PG故障；全部回滚后显式stale。
                    session.rollback()
                    conflict = True
                if conflict:
                    raise DomainError("discovery_stale", "目录发布与其他当前任务冲突")
            return
        if work["stage"] == "DETAILS" and not requested_ids:
            results = read_discovery_stage(None, work=work, requested_ids=requested_ids)
        else:
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=deadline,
                before_request=before_request,
            ) as gateway:
                results = read_discovery_stage(
                    gateway.accounts, work=work, requested_ids=requested_ids
                )
        with bounded_session(database_engine, task_deadline=deadline) as session:
            run, _, _ = locked_runtime_run(
                session,
                context=context,
                run_id=run_id,
                claim_id=claim_id,
                revision=revision,
            )
            stage_results(
                session,
                run=run,
                rows=results,
                schema_digest=work["schema_digest"],
                max_pages=2000
                if route.channel == "OFFICIAL_API"
                or results[0]["stage"] == "AUTHORIZED"
                else 1000,
            )
            advance_discovery_stage(run, results)
            run.claim_id = run.claimed_until = run.next_attempt_at = None
            run.revision += 1
            run.status = "RUNNING"
            run.error_code = None
            session.add(run)
            queue_runtime(session, run)
            session.commit()
    except Exception as error:
        _failure(
            database_engine,
            context=context,
            run_id=run_id,
            revision=revision,
            claim_id=claim_id if claimed else None,
            error=error,
        )


@celery_app.task(
    name=TASK_NAME,
    bind=True,
    time_limit=45,
    soft_time_limit=40,
    acks_late=True,
    reject_on_worker_lost=True,
)  # type: ignore[untyped-decorator]
def runtime_discover(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    limits = self.request.timelimit or (None, None)
    hard = limits[0] or self.time_limit
    process = current_process()
    if (
        not process.daemon
        or not process.name.startswith("ForkPoolWorker-")
        or self.request.called_directly
        or self.request.is_eager
        or type(hard) not in (int, float)
        or not 0 < hard <= 45
    ):
        raise DomainError(
            "discovery_worker_unbounded", "运行目录需要有界prefork后台任务"
        )
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        process_runtime_directory(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )
