"""One admitted official read per durable dispatch; production requires prefork."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.official.bootstrap import open_api_candidate_accounts
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred,
)
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.tenants.permissions import require_tenant

from .api_directory import SCHEMA_DIGEST, publish_api_directory
from .discovery import (
    locked_run,
    validate_run,
)
from .mcp_discovery_tasks import _detail_ids, read_discovery_stage, stage_results
from .models import (
    AuthorizationAttempt,
    DiscoveryRun,
    TikTokConnection,
)

register_dispatch_task("accounts.discover", "resources")
HARD_LIMIT_SECONDS = 45
RECOVERY_SECONDS = 60


def queue_run(
    session: Session,
    run: DiscoveryRun,
    *,
    suffix: str = "step",
    due: datetime | None = None,
) -> None:
    dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="tenant_admin"
        ),
        task_name="accounts.discover",
        task_key=f"discover:{run.id}:{run.revision}:{suffix}",
        payload={"run_id": str(run.id), "revision": run.revision},
    )
    if due:
        dispatch = session.get(PendingDispatch, dispatch_id)
        assert dispatch is not None
        dispatch.available_at = due


def start_discovery(session: Session, *, attempt_id: UUID) -> DiscoveryRun:
    attempt = session.get(AuthorizationAttempt, attempt_id)
    if attempt is None:
        raise DomainError("discovery_not_found", "未找到授权尝试")
    require_tenant(
        session, actor_id=attempt.actor_id, tenant_id=attempt.tenant_id, action="manage"
    )
    # Serialize generation creation on this tenant's connection only.
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == attempt.connection_id,
            TikTokConnection.tenant_id == attempt.tenant_id,
        )
        .with_for_update()
    ).one()
    existing = session.exec(
        select(DiscoveryRun).where(
            DiscoveryRun.tenant_id == attempt.tenant_id,
            DiscoveryRun.connection_id == connection.id,
            col(DiscoveryRun.status).in_(["RUNNING", "ADMISSION_WAIT"]),
        )
    ).first()
    if existing:
        if existing.candidate_attempt_id != attempt.id:
            raise DomainError("discovery_in_progress", "该连接已有发现任务")
        return existing
    previous = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.candidate_attempt_id == attempt.id)
        .order_by(col(DiscoveryRun.created_at).desc())
    ).first()
    if previous and previous.status == "COMPLETE":
        return previous
    if previous:
        run = previous
        run.status = "RUNNING"
        run.error_code = None
        run.revision += 1
    else:
        run = DiscoveryRun(
            tenant_id=attempt.tenant_id,
            actor_id=attempt.actor_id,
            connection_id=connection.id,
            candidate_attempt_id=attempt.id,
            credential_revision=attempt.base_credential_revision,
            work={"stage": "SUBJECT", "page": 1},
        )
        session.add(run)
    validate_run(session, run)
    session.flush()
    queue_run(session, run)
    return run


def _next_bc(session: Session, run: DiscoveryRun, after: str = "") -> str | None:
    value = session.execute(
        text("""SELECT min(item->>'bc_id') FROM discovery_staged_page p
        CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE p.run_id=:run_id AND p.stage='BCS' AND p.bc_id='' AND item->>'bc_id'>:after"""),
        {"run_id": run.id, "after": after},
    ).scalar_one()
    return value if isinstance(value, str) else None


def _apply(session: Session, run: DiscoveryRun, results: list[dict[str, Any]]) -> None:
    stage_results(session, run=run, rows=results, schema_digest=SCHEMA_DIGEST)
    work = copy.deepcopy(run.work)
    result = results[-1]
    stage = result["stage"]
    if stage == "SUBJECT":
        work.update(stage="AUTHORIZED", page=1)
    elif stage == "AUTHORIZED":
        work.update(stage="BCS", page=1)
    elif stage == "BCS":
        if result["last_page"]:
            bc_id = _next_bc(session, run)
            work.update(stage="ASSETS" if bc_id else "FINALIZE", page=1, bc_id=bc_id)
        else:
            work["page"] += 1
    elif stage == "ASSETS":
        work.update(
            stage="DETAILS",
            asset_last_page=result["last_page"],
            asset_total_pages=result["total_pages"],
        )
    elif stage == "DETAILS":
        work.update(
            stage="ROLES" if result["last_page"] else "ASSETS",
            page=1 if result["last_page"] else work["page"] + 1,
        )
        work.pop("asset_last_page", None)
        work.pop("asset_total_pages", None)
    elif stage == "ROLES":
        if result["last_page"]:
            bc_id = _next_bc(session, run, after=work["bc_id"])
            work.update(stage="ASSETS" if bc_id else "FINALIZE", page=1, bc_id=bc_id)
        else:
            work["page"] += 1
    run.work = work
    session.add(run)


def process_discovery(
    *,
    database_engine: Any,
    redis_client: Redis,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    """一次派发推进一个50条阶段；工厂独占物理请求、当前授权核验与准入。"""
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="tenant_admin")
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    if set(payload) == {"attempt_id"}:
        with bounded_session(database_engine, task_deadline=deadline) as session:
            attempt = session.get(AuthorizationAttempt, UUID(payload["attempt_id"]))
            if attempt is None or (attempt.tenant_id, attempt.actor_id) != (
                tenant_id,
                actor_id,
            ):
                raise DomainError("tenant_forbidden", "任务上下文不匹配")
            start_discovery(session, attempt_id=attempt.id)
            session.commit()
        return
    if set(payload) != {"run_id", "revision"} or type(payload["revision"]) is not int:
        raise DomainError("dispatch_payload_invalid", "目录任务参数无效")
    run_id = UUID(payload["run_id"])
    claim = uuid4()
    with bounded_session(database_engine, task_deadline=deadline) as session:
        run = locked_run(session, run_id)
        if (run.tenant_id, run.actor_id) != (tenant_id, actor_id):
            raise DomainError("tenant_forbidden", "任务上下文不匹配")
        if (
            run.status not in {"RUNNING", "ADMISSION_WAIT"}
            or run.revision != payload["revision"]
        ):
            return
        now = datetime.now(UTC)
        if (run.claimed_until and run.claimed_until > now) or (
            run.next_attempt_at and run.next_attempt_at > now
        ):
            return
        try:
            connection = validate_run(session, run)
            if connection.kind != "OFFICIAL_API" or run.candidate_attempt_id is None:
                raise DomainError(
                    "discovery_stale", "历史目录任务缺少可核实候选，请重新授权"
                )
        except DomainError as error:
            run.status, run.error_code = "ERROR", error.code
            session.add(run)
            session.commit()
            return
        work = copy.deepcopy(run.work)
        ids = _detail_ids(session, run=run) if work["stage"] == "DETAILS" else ()
        run.claim_id, run.claimed_until = (
            claim,
            now + timedelta(seconds=RECOVERY_SECONDS),
        )
        session.add(run)
        queue_run(session, run, suffix=f"recover:{claim}", due=run.claimed_until)
        session.commit()
    try:
        if work["stage"] == "FINALIZE":
            with bounded_session(database_engine, task_deadline=deadline) as session:
                publish_api_directory(session, context=context, run_id=run_id)
                session.commit()
            return
        if work["stage"] == "DETAILS" and not ids:
            results = read_discovery_stage(None, work=work, requested_ids=ids)
        else:
            with open_api_candidate_accounts(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                run_id=run_id,
                task_deadline=deadline,
            ) as gateway:
                results = read_discovery_stage(gateway, work=work, requested_ids=ids)
        with bounded_session(database_engine, task_deadline=deadline) as session:
            run = locked_run(session, run_id)
            if run.claim_id != claim or run.revision != payload["revision"]:
                return
            validate_run(session, run)
            _apply(session, run, results)
            run.claim_id = run.claimed_until = run.next_attempt_at = None
            run.revision += 1
            run.status = "RUNNING"
            session.add(run)
            queue_run(session, run)
            session.commit()
    except Exception as error:
        with bounded_session(
            database_engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
        ) as session:
            run = locked_run(session, run_id)
            if run.claim_id != claim:
                return
            run.claim_id = run.claimed_until = None
            run.error_code = (
                error.code
                if isinstance(error, DomainError)
                else "tiktok_response_error"
            )
            if isinstance(error, AccountAdmissionDeferred) or run.error_code in {
                "admission_unavailable",
                "tiktok_local_resources_unavailable",
            }:
                delay = (
                    error.retry_after_ms
                    if isinstance(error, AccountAdmissionDeferred)
                    else 5000
                )
                run.status = "ADMISSION_WAIT"
                run.next_attempt_at = datetime.now(UTC) + timedelta(
                    milliseconds=max(1, delay)
                )
                run.revision += 1
                queue_run(session, run, due=run.next_attempt_at)
            else:
                run.status = "ERROR"
            session.add(run)
            session.commit()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="accounts.discover",
    bind=True,
    time_limit=HARD_LIMIT_SECONDS,
    soft_time_limit=40,
)
def discover(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    # Celery solo/threads/gevent cannot enforce its prefork hard process limit.
    request_limits = self.request.timelimit or (None, None)
    effective_hard_limit = request_limits[0] or self.time_limit
    if (
        not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or self.request.called_directly
        or self.request.is_eager
        or isinstance(effective_hard_limit, bool)
        or not isinstance(effective_hard_limit, (int, float))
        or not 0 < effective_hard_limit <= HARD_LIMIT_SECONDS
    ):
        raise DomainError(
            "discovery_worker_unbounded", "账户发现需要启用 prefork 硬超时工作进程"
        )
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        process_discovery(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )


# Celery 已固定导入本模块；显式加载独立 MCP 候选 handler。
from .mcp_discovery_tasks import discover_mcp as discover_mcp  # noqa: E402
