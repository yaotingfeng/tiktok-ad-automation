"""One admitted official read per durable dispatch; production requires prefork."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process
from redis import Redis
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok import accounts as api
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred,
    admitted_account_call,
    official_client,
)
from app.jobs.admission import admission_policy
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.tenants.permissions import require_tenant

from .discovery import (
    claim_external_asset,
    finalize_directory,
    locked_run,
    save_directory_page,
    validate_run,
)
from .models import (
    AuthorizationAttempt,
    DiscoveryRun,
    DiscoverySeen,
    TenantBC,
    TikTokConnection,
)

register_dispatch_task("accounts.discover", "resources")
HARD_LIMIT_SECONDS = 45
RECOVERY_SECONDS = 60
PAGE_SIZE = 100
ENDPOINTS = {
    "AUTHORIZED": "/open_api/v1.3/oauth2/advertiser/get/",
    "BCS": "/open_api/v1.3/bc/get/",
    "ASSETS": "/open_api/v1.3/bc/asset/get/",
    "DETAILS": "/open_api/v1.3/advertiser/info/",
}


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
        )
        session.add(run)
    validate_run(session, run)
    session.flush()
    queue_run(session, run)
    return run


def _credential(session: Session, run: DiscoveryRun) -> str:
    connection = validate_run(session, run)
    if run.candidate_attempt_id:
        attempt = session.get(AuthorizationAttempt, run.candidate_attempt_id)
        assert attempt is not None
        ciphertext = attempt.candidate_ciphertext
    else:
        ciphertext = connection.credential_ciphertext
    if not ciphertext:
        raise DomainError("credential_invalid", "凭据缺少访问令牌")
    token = decrypt_credentials(tenant_id=run.tenant_id, ciphertext=ciphertext).get(
        "access_token"
    )
    if not token:
        raise DomainError("credential_invalid", "凭据缺少访问令牌")
    return token


def _read(client: Any, work: dict) -> dict:
    stage = work["stage"]
    if stage == "AUTHORIZED":
        return api.read_authorized_advertisers(
            client, app_id=settings.TIKTOK_APP_ID, secret=settings.TIKTOK_APP_SECRET
        )
    if stage == "BCS":
        return api.read_business_centers(client, page=work["page"], page_size=PAGE_SIZE)
    if stage == "ASSETS":
        return api.read_bc_assets(
            client,
            bc_id=work["bc_ids"][work["bc_index"]],
            page=work["page"],
            page_size=PAGE_SIZE,
        )
    return api.read_advertiser_details(
        client,
        advertiser_ids=[
            row["advertiser_id"]
            for row in work["rows"]
            if row["advertiser_id"] in work["authorized_ids"]
        ],
    )


def _apply(session: Session, run: DiscoveryRun, data: dict) -> None:
    work = copy.deepcopy(run.work)
    stage = work["stage"]
    if stage == "AUTHORIZED":
        work["authorized_ids"] = sorted(
            {api.external_id(row, "advertiser_id") for row in api.object_list(data)}
        )
        work.update(stage="BCS", page=1)
    elif stage == "BCS":
        rows, last = api.paged_rows(data, page=work["page"], page_size=PAGE_SIZE)
        for row in rows:
            bc_id, name = api.business_center(row)
            own = claim_external_asset(
                session, kind="BC", external_id=bc_id, tenant_id=run.tenant_id
            )
            bc = session.get(TenantBC, (run.tenant_id, bc_id))
            if bc is None:
                bc = TenantBC(tenant_id=run.tenant_id, bc_id=bc_id)
                session.add(bc)
            bc.name, bc.ownership_conflict = name, not own
            if bc_id not in work["bc_ids"]:
                work["bc_ids"].append(bc_id)
        session.add(
            DiscoverySeen(
                run_id=run.id,
                bc_id="",
                page=work["page"],
                last_page=last,
                processed_count=len(rows),
            )
        )
        if last:
            work.update(
                stage="ASSETS" if work["bc_ids"] else "FINALIZE", page=1, bc_index=0
            )
        else:
            work["page"] += 1
        run.bc_cursor = None if last else str(work["page"])
    elif stage == "ASSETS":
        rows, last = api.paged_rows(data, page=work["page"], page_size=PAGE_SIZE)
        work.update(rows=[api.asset_row(row) for row in rows], last_page=last)
        if any(row["advertiser_id"] in work["authorized_ids"] for row in work["rows"]):
            work["stage"] = "DETAILS"
        else:
            _save_assets(session, run, work, work["rows"])
    else:
        rows = [api.detail_row(row) for row in api.object_list(data)]
        details = {row["advertiser_id"]: row for row in rows}
        expected = {row["advertiser_id"] for row in work["rows"]}
        if len(details) != len(rows) or not set(details).issubset(expected):
            raise api.schema_error()
        # Missing detail records remain visible with missing metadata, never fabricated.
        merged = [
            {**row, **details.get(row["advertiser_id"], {})} for row in work["rows"]
        ]
        _save_assets(session, run, work, merged)
    run.work = work
    session.flush()
    if work["stage"] == "FINALIZE":
        finalize_directory(session, run_id=run.id)


def _save_assets(
    session: Session, run: DiscoveryRun, work: dict, rows: list[dict]
) -> None:
    save_directory_page(
        session,
        run_id=run.id,
        bc_id=work["bc_ids"][work["bc_index"]],
        page=work["page"],
        rows=rows,
        authorized_ids=set(work["authorized_ids"]),
        last_page=work["last_page"],
    )
    if work["last_page"]:
        work["bc_index"] += 1
        work["page"] = 1
    else:
        work["page"] += 1
    work["stage"] = "ASSETS" if work["bc_index"] < len(work["bc_ids"]) else "FINALIZE"
    work.pop("rows", None)
    work.pop("last_page", None)


def process_discovery(
    *,
    database_engine: Any,
    redis_client: Redis,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict,
) -> None:
    """Testable worker body. Caller must enforce a hard wallclock process deadline."""
    if "attempt_id" in payload:
        with Session(database_engine) as session, session.begin():
            attempt = session.get(AuthorizationAttempt, UUID(payload["attempt_id"]))
            if not attempt or (attempt.tenant_id, attempt.actor_id) != (
                tenant_id,
                actor_id,
            ):
                raise DomainError("tenant_forbidden", "任务上下文不匹配")
            start_discovery(session, attempt_id=attempt.id)
        return
    run_id = UUID(payload["run_id"])
    claim = uuid4()
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        run = locked_run(session, run_id)
        if (run.tenant_id, run.actor_id) != (tenant_id, actor_id):
            raise DomainError("tenant_forbidden", "任务上下文不匹配")
        if (
            run.status not in {"RUNNING", "ADMISSION_WAIT"}
            or payload.get("revision") != run.revision
        ):
            return
        if (run.claimed_until and run.claimed_until > now) or (
            run.next_attempt_at and run.next_attempt_at > now
        ):
            return
        try:
            token = _credential(session, run)
        except DomainError as error:
            run.status, run.error_code = "ERROR", error.code
            return
        work = copy.deepcopy(run.work)
        if work["stage"] == "FINALIZE":
            finalize_directory(session, run_id=run.id)
            return
        context = TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="tenant_admin"
        )
        run.claim_id, run.claimed_until = (
            claim,
            now + timedelta(seconds=RECOVERY_SECONDS),
        )
        # Durable recovery exists before network or process death.
        queue_run(session, run, suffix=f"recover:{claim}", due=run.claimed_until)
    try:
        endpoint = ENDPOINTS[work["stage"]]
        policy = admission_policy(endpoint)
        if policy.lease_ms <= (HARD_LIMIT_SECONDS + 5) * 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须长于工作进程硬超时及清理余量"
            )
        ids = [
            row["advertiser_id"]
            for row in work.get("rows", [])
            if row["advertiser_id"] in work["authorized_ids"]
        ]
        with admitted_account_call(
            redis_client,
            context=context,
            endpoint=endpoint,
            advertiser_id=ids[0] if len(ids) == 1 else "",
            policy=policy,
        ):
            with Session(database_engine) as session, session.begin():
                run = locked_run(session, run_id)
                if run.claim_id != claim:
                    return
                validate_run(session, run)
                run.sent_count += 1
            with official_client(access_token=token) as client:
                data = _read(client, work)
        with Session(database_engine) as session, session.begin():
            run = locked_run(session, run_id)
            if run.claim_id != claim:
                return
            validate_run(session, run)
            _apply(session, run, data)
            run.claim_id, run.claimed_until, run.next_attempt_at = None, None, None
            run.revision += 1
            if run.status != "COMPLETE":
                run.status = "RUNNING"
                queue_run(session, run)
    except Exception as error:
        # Never persist raw SDK exceptions, auth headers, query URLs or bodies.
        with Session(database_engine) as session, session.begin():
            run = locked_run(session, run_id)
            if run.claim_id != claim:
                return
            run.claim_id, run.claimed_until = None, None
            run.error_code = (
                error.code
                if isinstance(error, DomainError)
                else "tiktok_response_error"
            )
            if isinstance(error, AccountAdmissionDeferred) or (
                isinstance(error, DomainError) and error.code == "admission_unavailable"
            ):
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


@celery_app.task(
    name="accounts.discover",
    bind=True,
    time_limit=HARD_LIMIT_SECONDS,
    soft_time_limit=40,
)
def discover(self: Any, *, tenant_id: str, actor_id: str, payload: dict) -> None:
    # Celery solo/threads/gevent cannot enforce its prefork hard process limit.
    request_limits = self.request.timelimit or (None, None)
    effective_hard_limit = request_limits[0] or self.time_limit
    if (
        not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or self.request.called_directly
        or self.request.is_eager
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
