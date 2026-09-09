"""Durable, link-free BC role evidence. Each delivery reads 50 or publishes 100.

Public start owns no commit; workers close all DB transactions before SDK I/O.
Only complete distinct remote lists can establish capabilities. Local grants are
never invented from BC visibility or another connection's token.
"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import business_api_client as sdk  # type: ignore[import-untyped]
from billiard.process import current_process  # type: ignore[import-untyped]
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import admitted_account_call, checked_data, sdk_client
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.access import OPERABLE_REMOTE_STATUSES, usable_grants
from app.modules.accounts.capability_models import (
    CapabilityAsset,
    CapabilityJob,
    CapabilityPage,
    CapabilityRequest,
)
from app.modules.accounts.capability_schemas import CapabilityEvidence
from app.modules.accounts.directory_models import DirectoryRevision
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.permissions import require_tenant

TASK_NAME = "accounts.refresh_capabilities"
ENDPOINT = "/open_api/v1.3/bc/asset/get/"
REVISION = "bc-token-roles-2026-09-09-v2"
HARD_LIMIT = 45
CLAIM_SECONDS = 60
REPAIR_SECONDS = 120
register_dispatch_task(TASK_NAME, "resources")


def _require_bounded_worker() -> None:
    process = current_process()
    request = getattr(current_task, "request", None)
    limits = getattr(request, "timelimit", None)
    hard = limits[0] if isinstance(limits, (tuple, list)) and limits else None
    if hard is None:
        hard = getattr(current_task, "time_limit", None)
    if (
        not request
        or getattr(request, "called_directly", True)
        or getattr(request, "is_eager", True)
        or not process.daemon
        or not process.name.startswith("ForkPoolWorker-")
        or isinstance(hard, bool)
        or not isinstance(hard, (int, float))
        or not 0 < hard <= HARD_LIMIT
    ):
        raise DomainError("capability_worker_unbounded", "能力重检需要有界后台任务")


def _connection(
    session: Session,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
    *,
    lock: bool,
) -> TikTokConnection:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    # One lock order everywhere: connection, job, dispatch/grants. No lock survives I/O.
    statement = select(TikTokConnection).where(
        TikTokConnection.tenant_id == context.tenant_id,
        TikTokConnection.id == connection_id,
    )
    if lock:
        statement = statement.with_for_update()
    conn = session.exec(statement.execution_options(populate_existing=True)).first()
    bc = session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
    if conn is None or conn.status != "ACTIVE" or not conn.credential_ciphertext:
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    if bc is None or bc.ownership_conflict:
        raise DomainError("account_not_in_bc", "当前租户 BC 不可用")
    if (
        session.exec(
            usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
            .where(BCAccountAccess.connection_id == connection_id)
            .limit(1)
        ).first()
        is None
    ):
        raise DomainError(
            "capability_unavailable", "当前连接在此 BC 没有可核实的授权账户"
        )
    return conn


def _directory_basis(
    session: Session, context: TenantContext, bc_id: str, connection_id: UUID
) -> str | None:
    # Triggers fence every directory mutation in its own transaction. A pure
    # primary-key read replaces the former all-account aggregate for each scene.
    # Missing state fails closed; readers never lazily create or repair a fence.
    with session.no_autoflush:
        value = session.exec(
            select(DirectoryRevision.revision).where(
                DirectoryRevision.tenant_id == context.tenant_id,
                DirectoryRevision.bc_id == bc_id,
                DirectoryRevision.connection_id == connection_id,
            )
        ).first()
    if value is None:
        return None
    return sha256(
        json.dumps(
            [
                REVISION,
                settings.BC_CAPABILITY_MAX_AGE_SECONDS,
                str(context.tenant_id),
                bc_id,
                str(connection_id),
                value,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _scope_flags(conn: TikTokConnection) -> tuple[bool, bool, bool]:
    private = decrypt_credentials(
        tenant_id=conn.tenant_id, ciphertext=conn.credential_ciphertext or ""
    )
    try:
        values = json.loads(private["scope"])
        if (
            not isinstance(values, list)
            or len(values) > 1024
            or any(type(x) is not int or not 0 < x < 2**64 for x in values)
        ):
            return False, False, False
    except KeyError, ValueError, TypeError:
        return False, False, False
    # Official doc1753986142651394: parent 2, creative/video/upload 6/61/611.
    scopes = set(values)
    return True, 2 in scopes, bool(scopes & {6, 61, 611})


def _queue(session: Session, job: CapabilityJob, *, delay: int = 0) -> None:
    now = datetime.now(UTC)
    job.due_at = now + timedelta(seconds=delay)
    job.repair_after = job.due_at + timedelta(seconds=REPAIR_SECONDS)
    job.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=job.tenant_id, actor_id=job.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"capability:{job.id}:{job.revision}",
        payload={"job_id": str(job.id), "revision": job.revision},
    )
    dispatch = session.get(PendingDispatch, job.dispatch_id)
    assert dispatch is not None
    dispatch.available_at = job.due_at
    session.add_all([job, dispatch])


def start_capability_refresh(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    connection_id: UUID,
    request_id: UUID,
) -> UUID:
    conn = _connection(session, context, bc_id, connection_id, lock=True)
    previous = session.get(CapabilityRequest, (context.tenant_id, bc_id, request_id))
    if previous:
        original = session.get(CapabilityJob, previous.job_id)
        assert original
        if original.connection_id != connection_id:
            raise DomainError("request_id_conflict", "同一请求不能更换授权连接")
        return original.id
    basis = _directory_basis(session, context, bc_id, connection_id)
    if basis is None:
        raise DomainError("capability_unavailable", "当前账户目录缺少可核实版本")
    now = datetime.now(UTC)
    existing = session.exec(
        select(CapabilityJob)
        .where(
            CapabilityJob.tenant_id == context.tenant_id,
            CapabilityJob.bc_id == bc_id,
            CapabilityJob.connection_id == connection_id,
            CapabilityJob.credential_version == conn.credential_version,
            CapabilityJob.directory_basis == basis,
            (col(CapabilityJob.status) == "PENDING")
            | (
                (col(CapabilityJob.status) == "COMPLETE")
                & (col(CapabilityJob.expires_at) > now)
            ),
        )
        .order_by(col(CapabilityJob.created_at).desc())
        .limit(1)
    ).first()
    try:
        with session.begin_nested():
            if existing is None:
                existing = CapabilityJob(
                    tenant_id=context.tenant_id,
                    bc_id=bc_id,
                    connection_id=connection_id,
                    actor_id=context.actor_id,
                    credential_version=conn.credential_version,
                    directory_basis=basis,
                )
                session.add(existing)
                session.flush()
                _queue(session, existing)
            session.add(
                CapabilityRequest(
                    tenant_id=context.tenant_id,
                    bc_id=bc_id,
                    request_id=request_id,
                    job_id=existing.id,
                )
            )
            session.flush()
    except IntegrityError:
        # Competing connections can race the same request UUID. Roll back the
        # entire candidate job/outbox savepoint, then return the exact winner.
        previous = session.get(
            CapabilityRequest,
            (context.tenant_id, bc_id, request_id),
            populate_existing=True,
        )
        if previous is None:
            raise
        original = session.get(CapabilityJob, previous.job_id)
        assert original
        if original.connection_id != connection_id:
            raise DomainError(
                "request_id_conflict", "同一请求不能更换授权连接"
            ) from None
        return original.id

    return existing.id


def get_capability_job(
    session: Session, *, context: TenantContext, bc_id: str, job_id: UUID
) -> CapabilityJob:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    job = session.exec(
        select(CapabilityJob)
        .where(
            CapabilityJob.tenant_id == context.tenant_id,
            CapabilityJob.bc_id == bc_id,
            CapabilityJob.id == job_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise DomainError("capability_not_found", "当前租户 BC 能力任务不存在")
    return job


def _parse(response: object, page: int) -> tuple[list[tuple[str, str]], int, int]:
    data = checked_data(response)
    info, values = data.get("page_info"), data.get("list")
    invalid = DomainError("capability_response_unverified", "账户角色分页无法完整核实")
    if not isinstance(info, dict) or not isinstance(values, list) or len(values) > 50:
        raise invalid
    if (
        any(
            type(info.get(k)) is not int
            for k in ("page", "page_size", "total_page", "total_number")
        )
        or info["page"] != page
        or info["page_size"] != 50
        or page < 1
        or info["total_page"] < 0
        # Storage bound, not a platform/account-count policy. A 1000-page cap
        # incorrectly rejects valid 100k-account directories at 50 rows/page.
        or not 0 <= info["total_number"] <= 2**31 - 1
        or page > max(1, info["total_page"])
    ):
        raise invalid
    if info["total_page"] != (info["total_number"] + 49) // 50 or len(values) != min(
        50, max(0, info["total_number"] - (page - 1) * 50)
    ):
        raise invalid
    rows = []
    seen = set()
    for item in values:
        if not isinstance(item, dict):
            raise invalid
        aid, role = item.get("asset_id"), item.get("advertiser_role")
        if (
            not isinstance(aid, str)
            or not aid.strip()
            or len(aid) > 128
            or aid in seen
            or item.get("asset_type") != "ADVERTISER"
            or not isinstance(role, str)
            or role not in {"ADMIN", "OPERATOR", "ANALYST"}
        ):
            raise invalid
        seen.add(aid)
        rows.append((aid, role))
    return rows, info["total_page"], info["total_number"]


def _locked_job(
    session: Session, tenant_id: UUID, job_id: UUID
) -> CapabilityJob | None:
    # Load identity without locks before taking the consistent parent-first locks.
    identity = session.exec(
        select(CapabilityJob.connection_id).where(
            CapabilityJob.tenant_id == tenant_id, CapabilityJob.id == job_id
        )
    ).first()
    if identity is None:
        return None
    session.exec(
        select(TikTokConnection)
        .where(TikTokConnection.tenant_id == tenant_id, TikTokConnection.id == identity)
        .with_for_update()
    ).first()
    return session.exec(
        select(CapabilityJob)
        .where(CapabilityJob.tenant_id == tenant_id, CapabilityJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _validate(
    session: Session, job: CapabilityJob, context: TenantContext
) -> TikTokConnection:
    if job.expires_at is not None and job.expires_at <= datetime.now(UTC):
        raise DomainError("capability_stale", "账户角色证据已过期，需要重新重检")
    conn = _connection(session, context, job.bc_id, job.connection_id, lock=False)
    if (
        conn.credential_version != job.credential_version
        or _directory_basis(session, context, job.bc_id, job.connection_id)
        != job.directory_basis
    ):
        raise DomainError("capability_stale", "授权或账户目录已改变，需要重新重检")
    return conn


def _publish(session: Session, job: CapabilityJob, context: TenantContext) -> None:
    now = datetime.now(UTC)
    if job.expires_at is None or job.expires_at <= now:
        raise DomainError("capability_stale", "完整账户角色证据已过期")
    # Join exact connection-local grants and their complete role facts. No N+1 lookup.
    statement = (
        select(BCAccountAccess, AdvertiserAccount, CapabilityAsset.role)
        .join(
            AdvertiserAccount,
            (col(AdvertiserAccount.tenant_id) == BCAccountAccess.tenant_id)
            & (col(AdvertiserAccount.advertiser_id) == BCAccountAccess.advertiser_id),
        )
        .outerjoin(
            CapabilityAsset,
            (col(CapabilityAsset.job_id) == job.id)
            & (col(CapabilityAsset.advertiser_id) == BCAccountAccess.advertiser_id),
        )
        .where(
            BCAccountAccess.tenant_id == context.tenant_id,
            BCAccountAccess.bc_id == job.bc_id,
            BCAccountAccess.connection_id == job.connection_id,
        )
    )
    if job.publish_after is not None:
        statement = statement.where(BCAccountAccess.advertiser_id > job.publish_after)
    rows = session.exec(
        statement.order_by(col(BCAccountAccess.advertiser_id))
        .limit(100)
        .with_for_update(of=BCAccountAccess)
        .execution_options(populate_existing=True)
    ).all()
    for grant, account, role in rows:
        eligible = (
            grant.in_bc
            and grant.authorized
            and grant.active
            and not account.ownership_conflict
            and bool(account.currency.strip())
            and bool(account.timezone.strip())
        )
        known = job.scope_known and role is not None and eligible
        operate = (
            known
            and role in {"ADMIN", "OPERATOR"}
            and account.remote_status in OPERABLE_REMOTE_STATUSES
        )
        grant.permission_state = "VERIFIED" if known else "UNKNOWN"
        grant.can_build = bool(operate and job.scope_build)
        grant.can_upload = bool(operate and job.scope_upload)
        grant.checked_at = now
        session.add(grant)
    job.published_count += len(rows)
    if rows:
        job.publish_after = rows[-1][0].advertiser_id
    if len(rows) < 100:
        job.status, job.phase, job.completed_at = "COMPLETE", "DONE", now
        job.error_code = None if job.scope_known else "capability_scope_unknown"
    else:
        job.revision += 1
        _queue(session, job)
    session.add(job)


def _fail(session: Session, job: CapabilityJob, error: Exception) -> None:
    code = (
        error.code
        if isinstance(error, DomainError)
        else "capability_remote_unavailable"
    )
    if code == "capability_stale":
        status = "STALE"
    elif code in {
        "action_forbidden",
        "tenant_forbidden",
        "connection_unavailable",
        "account_not_in_bc",
        "capability_unavailable",
        "credential_invalid",
    }:
        status = "BLOCKED"
    elif code in {"capability_response_unverified", "tiktok_response_error"}:
        status = "FAILED"
    else:
        # GET transport/admission failures are safe to retry; same business job.
        code = (
            code
            if code
            in {
                "admission_deferred",
                "admission_unavailable",
                "admission_policy_invalid",
                "admission_unconfigured",
            }
            else "capability_remote_unavailable"
        )
        status = "PENDING"
    job.claim_token = job.claimed_until = None
    job.error_code, job.status = code, status
    if status == "PENDING":
        job.failure_count += 1
        job.revision += 1
        _queue(session, job, delay=min(300, 2 ** min(job.failure_count, 8)))
    session.add(job)


def process_capability(
    *,
    database_engine: Any,
    redis_client: Any,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    _require_bounded_worker()
    if (
        set(payload) != {"job_id", "revision"}
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "能力任务参数无效")
    try:
        job_id = UUID(payload["job_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "能力任务标识无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="operator")
    nonce = uuid4()
    with Session(database_engine) as session, session.begin():
        job = _locked_job(session, tenant_id, job_id)
        if job is None or job.actor_id != actor_id:
            return
        now = datetime.now(UTC)
        if (
            job.status != "PENDING"
            or job.revision != payload["revision"]
            or job.due_at > now
            or (job.claimed_until and job.claimed_until > now)
        ):
            return
        try:
            with session.begin_nested():
                _validate(session, job, context)
                if job.phase == "PUBLISH":
                    _publish(session, job, context)
                    return
                policy = admission_policy(ENDPOINT)
                if policy.lease_ms <= (HARD_LIMIT + 5) * 1000:
                    raise DomainError(
                        "admission_policy_invalid", "能力任务调用租约短于工作进程硬限"
                    )
                job.claim_token = nonce
                job.claimed_until = now + timedelta(seconds=CLAIM_SECONDS)
                session.add(job)
                session.flush()
        except Exception as error:
            _fail(session, job, error)
            return
        page, connection_id, bc_id = job.next_page, job.connection_id, job.bc_id
    try:
        with admitted_account_call(
            redis_client,
            context=context,
            endpoint=ENDPOINT,
            advertiser_id=bc_id,
            policy=policy,
        ):
            with Session(database_engine) as session:
                job = session.get(CapabilityJob, job_id)
                assert job
                _validate(session, job, context)
                with sdk_client(
                    session, context=context, connection_id=connection_id
                ) as client:
                    session.close()
                    try:
                        response = sdk.BCApi(client).bc_asset_get(
                            bc_id,
                            "ADVERTISER",
                            client.default_headers["Access-Token"],
                            page=page,
                            page_size=50,
                            _request_timeout=(5, 30),
                        )
                    except ApiException as error:
                        # Inspect only the structured HTTP status before sdk_client
                        # sanitizes errors. A throttled GET must release its worker
                        # and enter the existing durable retry/backoff path.
                        if error.status == 429:
                            raise DomainError(
                                "capability_remote_unavailable",
                                "账户能力读取暂不可用",
                                retryable=True,
                            ) from None
                        raise
        rows, total_pages, total_count = _parse(response, page)
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                job is None
                or job.actor_id != actor_id
                or job.connection_id != connection_id
                or job.bc_id != bc_id
                or job.status != "PENDING"
                or job.claim_token != nonce
                or job.revision != payload["revision"]
                or job.claimed_until is None
                or job.claimed_until <= datetime.now(UTC)
            ):
                return
            conn = _validate(session, job, context)
            if job.total_pages is not None and (
                job.total_pages != total_pages or job.total_count != total_count
            ):
                raise DomainError(
                    "capability_response_unverified", "账户角色分页总数已变化"
                )
            job.total_pages, job.total_count = total_pages, total_count
            if (
                rows
                and session.exec(
                    select(CapabilityAsset.advertiser_id)
                    .where(
                        CapabilityAsset.job_id == job.id,
                        col(CapabilityAsset.advertiser_id).in_(
                            [aid for aid, _ in rows]
                        ),
                    )
                    .limit(1)
                ).first()
                is not None
            ):
                raise DomainError(
                    "capability_response_unverified", "账户角色分页包含重复记录"
                )
            observed_at = datetime.now(UTC)
            if page == 1:
                job.expires_at = observed_at + timedelta(
                    seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS
                )
            session.add(
                CapabilityPage(
                    tenant_id=tenant_id,
                    bc_id=bc_id,
                    job_id=job.id,
                    page=page,
                    row_count=len(rows),
                    observed_at=observed_at,
                )
            )
            session.flush()
            for aid, role in rows:
                session.add(
                    CapabilityAsset(
                        tenant_id=tenant_id,
                        bc_id=bc_id,
                        job_id=job.id,
                        page=page,
                        advertiser_id=aid,
                        role=role,
                    )
                )
            session.flush()
            job.seen_count += len(rows)
            job.next_page += 1
            job.claim_token = job.claimed_until = None
            job.failure_count = 0
            job.error_code = None
            if page >= max(1, total_pages):
                if job.seen_count != total_count:
                    raise DomainError(
                        "capability_response_unverified", "账户角色分页未完整读取"
                    )
                job.scope_known, job.scope_build, job.scope_upload = _scope_flags(conn)
                job.phase = "PUBLISH"
            job.revision += 1
            _queue(session, job)
    except Exception as error:
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                job
                and job.actor_id == actor_id
                and job.claim_token == nonce
                and job.revision == payload["revision"]
            ):
                _fail(session, job, error)


def repair_capabilities(*, database_engine: Any, limit: int = 100) -> int:
    """Repair exact current transport identities; preserve unsent broker backoff."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session:
        identities = session.exec(
            select(CapabilityJob.tenant_id, CapabilityJob.id)
            .where(CapabilityJob.status == "PENDING", CapabilityJob.repair_after <= now)
            .order_by(col(CapabilityJob.repair_after), col(CapabilityJob.id))
            .limit(limit)
        ).all()
    repaired = 0
    for tenant_id, job_id in identities:
        with Session(database_engine) as session, session.begin():
            job = _locked_job(session, tenant_id, job_id)
            if (
                not job
                or job.status != "PENDING"
                or job.repair_after > now
                or (job.claimed_until and job.claimed_until > now)
            ):
                continue
            job.claim_token = job.claimed_until = None
            dispatch = session.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == job.dispatch_id)
                .with_for_update()
            ).first()
            if dispatch is None:
                _queue(session, job)
            elif (
                dispatch.tenant_id != job.tenant_id
                or dispatch.actor_id != job.actor_id
                or dispatch.task_name != TASK_NAME
                or dispatch.payload != {"job_id": str(job.id), "revision": job.revision}
                or dispatch.task_key != f"capability:{job.id}:{job.revision}"
            ):
                job.status, job.error_code = "FAILED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at = None
                dispatch.available_at = max(now, job.due_at)
                session.add(dispatch)
            job.repair_after = now + timedelta(seconds=REPAIR_SECONDS)
            session.add(job)
            repaired += 1
    return repaired


def get_capability_evidence(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    connection_id: UUID,
) -> CapabilityEvidence | None:
    """Pure local evidence read; never queues, decrypts, locks, flushes or grants."""
    with session.no_autoflush:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="read",
        )
        conn = session.get(TikTokConnection, connection_id, populate_existing=True)
        if (
            conn is None
            or conn.tenant_id != context.tenant_id
            or conn.status != "ACTIVE"
        ):
            return None
        grant = session.exec(
            usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
            .where(
                BCAccountAccess.advertiser_id == advertiser_id,
                BCAccountAccess.connection_id == connection_id,
            )
            .execution_options(populate_existing=True)
        ).first()
        if grant is None:
            return None
        now = datetime.now(UTC)
        statement = (
            select(CapabilityJob, CapabilityAsset.page, CapabilityAsset.role)
            .outerjoin(
                CapabilityAsset,
                (col(CapabilityAsset.job_id) == CapabilityJob.id)
                & (col(CapabilityAsset.advertiser_id) == advertiser_id),
            )
            .where(
                CapabilityJob.tenant_id == context.tenant_id,
                CapabilityJob.bc_id == bc_id,
                CapabilityJob.connection_id == connection_id,
                CapabilityJob.credential_version == conn.credential_version,
                CapabilityJob.status == "COMPLETE",
                CapabilityJob.phase == "DONE",
                col(CapabilityJob.expires_at) > now,
            )
            .order_by(col(CapabilityJob.completed_at).desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        row = session.exec(statement).first()
        if row is None:
            return None
        job, page, role = row
        if (
            _directory_basis(session, context, bc_id, connection_id)
            != job.directory_basis
        ):
            return None
        account = session.get(
            AdvertiserAccount,
            (context.tenant_id, advertiser_id),
            populate_existing=True,
        )
        proof_page = session.get(CapabilityPage, (job.id, page or 1))
        assert account and proof_page and job.expires_at
        operable = (
            job.scope_known
            and grant.permission_state == "VERIFIED"
            and role in {"ADMIN", "OPERATOR"}
            and account.remote_status in OPERABLE_REMOTE_STATUSES
            and bool(account.currency.strip())
            and bool(account.timezone.strip())
        )
        return CapabilityEvidence(
            job_id=job.id,
            evidence_ids=(job.id,),
            page_number=page,
            observed_at=proof_page.observed_at,
            expires_at=job.expires_at,
            scope_verified=job.scope_known,
            can_build=bool(operable and job.scope_build and grant.can_build),
            can_upload=bool(operable and job.scope_upload and grant.can_upload),
            source_revision=REVISION,
            basis_digest=job.directory_basis,
        )
