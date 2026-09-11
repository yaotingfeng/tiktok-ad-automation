"""管理员显式授权的历史只读核查；绝不改变原步骤或续建其子步骤。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, text
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.builds import (
    AdGroupStatus,
    BuildPage,
    BuildReadQuery,
)
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.builds.execution_admission import admitted_build_call
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.reconciliation import (
    _known_ids,
    original_create_attempt,
    require_bounded_worker,
    source_intent,
)
from app.modules.builds.reconciliation_scan import advance
from app.modules.builds.recovery_models import HistoricalReadReceipt
from app.modules.builds.recovery_routes import (
    BuildHistoricalRead,
    BuildHistoricalReadPage,
)
from app.modules.builds.routes import load_preview_route
from app.modules.tenants.permissions import require_tenant

TASK_NAME = "builds.historical_read_step"
register_dispatch_task(TASK_NAME, "resources")
WAIT_ERRORS = {
    "mcp_refresh_pending",
    "gateway_credentials_changed",
    "admission_unavailable",
    "tiktok_local_resources_unavailable",
    "tiktok_call_deadline_exceeded",
}
SAFE_ERRORS = WAIT_ERRORS | {
    "historical_read_scope_unverified",
    "historical_read_source_changed",
    "historical_read_expired",
    "historical_read_claim_lost",
    "readback_response_unknown",
    "readback_intent_incomplete",
    "route_authorization_changed",
    "route_contract_changed",
    "route_evidence_stale",
    "account_access_denied",
    "action_forbidden",
    "connection_unavailable",
    "mcp_refresh_unknown",
    "mcp_refresh_reauth_required",
}


def _digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _manage(session: Session, context: TenantContext) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage"
    )


def _source(
    session: Session, context: TenantContext, identity: UUID
) -> tuple[ExecutionStep, BuildUnit, FrozenTikTokRoute]:
    source = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.tenant_id == context.tenant_id, ExecutionStep.id == identity
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if source is None:
        raise DomainError("resource_not_found", "原执行步骤不存在")
    unit = session.get(BuildUnit, source.unit_id)
    if unit is None or (unit.tenant_id, unit.preview_id, unit.bc_id) != (
        context.tenant_id,
        source.preview_id,
        source.bc_id,
    ):
        raise DomainError("historical_read_source_changed", "原执行范围无法核实")
    old = load_preview_route(session, context=context, preview_id=source.preview_id)
    if (
        (old.connection_id, old.bc_id) != (unit.connection_id, unit.bc_id)
        or source.kind not in {"CTA", "CAMPAIGN", "ADGROUP", "AD"}
        or source.status not in {"UNKNOWN", "SUCCEEDED"}
        or not source.request_body
        or _digest(source.request_body) != source.request_body_digest
    ):
        raise DomainError("historical_read_source_changed", "原创建证据无法核实")
    return source, unit, old


def _proof(
    session: Session,
    context: TenantContext,
    old: FrozenTikTokRoute,
    new: FrozenTikTokRoute,
    advertiser_id: str,
) -> dict[str, Any]:
    if (old.tenant_id, old.bc_id, old.connection_id, old.channel) != (
        new.tenant_id,
        new.bc_id,
        new.connection_id,
        new.channel,
    ) or new.authorization_revision <= old.authorization_revision:
        raise DomainError(
            "historical_read_scope_unverified", "仅允许同连接的新授权只读核查"
        )
    verify_route(
        session,
        context=context,
        route=new,
        advertiser_id=advertiser_id,
        capability="read",
    )
    values = []
    for route in (old, new):
        authorization = session.exec(
            select(ConnectionAuthorization)
            .where(
                ConnectionAuthorization.tenant_id == context.tenant_id,
                ConnectionAuthorization.connection_id == route.connection_id,
                ConnectionAuthorization.authorization_revision
                == route.authorization_revision,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            authorization is None
            or not authorization.source
            or authorization.source == "UNKNOWN"
            or authorization.verified_at is None
            or authorization.verified_at.tzinfo is None
            or any(
                type(value) is not str or not value.strip()
                for value in (
                    authorization.upstream_subject,
                    authorization.issuer,
                    authorization.resource,
                )
            )
        ):
            raise DomainError(
                "historical_read_scope_unverified", "上游主体或授权资源尚未明确核实"
            )
        values.append(authorization)
    prior, current = values
    if (
        (prior.upstream_subject, prior.issuer, prior.resource)
        != (current.upstream_subject, current.issuer, current.resource)
        or not current.scopes
        or any(type(scope) is not str or not scope.strip() for scope in current.scopes)
    ):
        raise DomainError(
            "historical_read_scope_unverified", "当前授权未证明同主体与资源的读取范围"
        )
    grant = session.get(
        BCAccountAccess,
        (context.tenant_id, new.bc_id, advertiser_id, new.connection_id),
    )
    assert grant and grant.checked_at and prior.verified_at and current.verified_at
    return {
        "subject": current.upstream_subject,
        "issuer": current.issuer,
        "resource": current.resource,
        "old_authorization_id": str(prior.id),
        "new_authorization_id": str(current.id),
        "scopes_digest": _digest(sorted(current.scopes)),
        "source": current.source,
        "read_authorized": True,
        "old_verified_at": prior.verified_at.isoformat(),
        "new_verified_at": current.verified_at.isoformat(),
        "grant_checked_at": grant.checked_at.isoformat(),
    }


def _request(
    session: Session, context: TenantContext, request_id: UUID, source_step_id: UUID
) -> BuildHistoricalRead | None:
    row = session.exec(
        select(BuildHistoricalRead).where(
            BuildHistoricalRead.tenant_id == context.tenant_id,
            BuildHistoricalRead.request_id == request_id,
        )
    ).one_or_none()
    if row is not None and row.source_step_id != source_step_id:
        raise DomainError("idempotency_conflict", "核查请求已绑定其他步骤")
    return row


def _queue(session: Session, row: BuildHistoricalRead, *, delay: int = 0) -> None:
    row.due_at = datetime.now(UTC) + timedelta(seconds=delay)
    row.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=row.tenant_id, actor_id=row.request_actor_id, role="admin"
        ),
        task_name=TASK_NAME,
        task_key=f"historical-read:{row.id}:{row.dispatch_revision}",
        payload={"read_id": str(row.id), "revision": row.dispatch_revision},
    )
    dispatch = session.get(PendingDispatch, row.dispatch_id)
    assert dispatch
    dispatch.available_at = row.due_at
    row.repair_after = row.due_at + timedelta(seconds=120)
    session.add_all([row, dispatch])


def authorize_historical_read(
    session: Session,
    *,
    context: TenantContext,
    source_step_id: UUID,
    new_route: FrozenTikTokRoute,
    request_id: UUID | None = None,
) -> UUID:
    _manage(session, context)
    request_id = request_id or uuid4()
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"historical-read:{context.tenant_id}:{request_id}"},
    )
    old_request = _request(session, context, request_id, source_step_id)
    if old_request is not None:
        return old_request.id
    source, unit, old_route = _source(session, context, source_step_id)
    proof = _proof(session, context, old_route, new_route, unit.advertiser_id)
    count, attempt_id = original_create_attempt(session, source)
    source_intent(session, source, old_route)
    ids = _known_ids(session, source)
    if len(ids) > 1 or (source.kind == "CTA" and not ids):
        raise DomainError("historical_read_source_changed", "原对象身份存在歧义")
    row = BuildHistoricalRead(
        tenant_id=context.tenant_id,
        request_id=request_id,
        submission_id=source.submission_id,
        source_step_id=source.id,
        source_attempt=count,
        source_attempt_id=attempt_id,
        connection_id=old_route.connection_id,
        bc_id=old_route.bc_id,
        advertiser_id=unit.advertiser_id,
        old_authorization_revision=old_route.authorization_revision,
        new_authorization_revision=new_route.authorization_revision,
        request_actor_id=context.actor_id,
        source_request_digest=source.request_body_digest,
        old_route=old_route.model_dump(mode="json"),
        new_route=new_route.model_dump(mode="json"),
        authorization_proof=proof,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        progress={
            "scan_id": str(uuid4()),
            "page": 1,
            "stage": "OBJECT",
            "seen": 0,
            "matches": 0,
            "known_id": next(iter(ids), None),
        },
    )
    session.add(row)
    session.flush()
    _queue(session, row)
    session.flush()
    return row.id


def request_historical_read(
    session: Session, *, context: TenantContext, source_step_id: UUID, request_id: UUID
) -> UUID:
    _manage(session, context)
    SASession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"historical-read:{context.tenant_id}:{request_id}"},
    )
    old_request = _request(session, context, request_id, source_step_id)
    if old_request is not None:
        return old_request.id
    _, unit, old = _source(session, context, source_step_id)
    current = freeze_route(
        session, context=context, bc_id=unit.bc_id, connection_id=old.connection_id
    )
    return authorize_historical_read(
        session,
        context=context,
        source_step_id=source_step_id,
        new_route=current,
        request_id=request_id,
    )


def _row(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> BuildHistoricalRead:
    query = select(BuildHistoricalRead).where(
        BuildHistoricalRead.tenant_id == context.tenant_id,
        BuildHistoricalRead.id == identity,
    )
    if lock:
        query = query.with_for_update()
    row = session.exec(query.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "历史核查不存在")
    return row


def historical_read_receipt(
    session: Session, *, context: TenantContext, read_id: UUID
) -> HistoricalReadReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = _row(session, context, read_id)
    return HistoricalReadReceipt.model_validate(
        {
            "read_id": row.id,
            "request_id": row.request_id,
            "source_step_id": row.source_step_id,
            "state": row.status,
            "remote_id": row.remote_id,
            "mismatch": row.mismatch,
            "reason_code": row.error_code,
            "requires_new_preparation": True,
        }
    )


def historical_read_request_receipt(
    session: Session, *, context: TenantContext, request_id: UUID
) -> HistoricalReadReceipt:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    row = session.exec(
        select(BuildHistoricalRead).where(
            BuildHistoricalRead.tenant_id == context.tenant_id,
            BuildHistoricalRead.request_id == request_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "历史核查请求不存在")
    return historical_read_receipt(session, context=context, read_id=row.id)


ELIGIBLE = """
SELECT s.id FROM execution_step s
JOIN build_unit u ON u.tenant_id=s.tenant_id AND u.preview_id=s.preview_id AND u.id=s.unit_id
JOIN build_route_context r ON r.tenant_id=s.tenant_id AND r.preview_id=s.preview_id AND r.bc_id=s.bc_id AND r.connection_id=u.connection_id
JOIN tiktok_connection c ON c.tenant_id=r.tenant_id AND c.id=r.connection_id AND c.kind=r.channel
JOIN bc_connection_binding b ON b.tenant_id=r.tenant_id AND b.bc_id=r.bc_id AND b.connection_id=r.connection_id AND b.kind=r.channel
JOIN tenant_bc bc ON bc.tenant_id=r.tenant_id AND bc.bc_id=r.bc_id
JOIN connection_authorization old ON old.tenant_id=r.tenant_id AND old.connection_id=r.connection_id AND old.authorization_revision=r.authorization_revision
JOIN connection_authorization new ON new.tenant_id=c.tenant_id AND new.connection_id=c.id AND new.authorization_revision=c.authorization_revision
JOIN bc_account_access a ON a.tenant_id=u.tenant_id AND a.bc_id=u.bc_id AND a.connection_id=u.connection_id AND a.advertiser_id=u.advertiser_id
JOIN advertiser_account aa ON aa.tenant_id=a.tenant_id AND aa.advertiser_id=a.advertiser_id
WHERE s.tenant_id=:tenant AND s.kind IN ('CTA','CAMPAIGN','ADGROUP','AD')
AND (s.status='UNKNOWN' OR s.status='SUCCEEDED' AND s.mismatch)
AND s.request_body IS NOT NULL AND s.request_body_digest IS NOT NULL
AND s.request_body->>'advertiser_id'=u.advertiser_id
AND c.status='ACTIVE' AND c.authorization_revision>r.authorization_revision
AND NOT bc.ownership_conflict AND NOT aa.ownership_conflict
AND trim(aa.currency)<>'' AND trim(aa.timezone)<>''
AND a.in_bc AND a.authorized AND a.active AND a.checked_at BETWEEN :cutoff AND :now
AND old.source<>'UNKNOWN' AND new.source<>'UNKNOWN'
AND old.source<>'' AND new.source<>'' AND old.verified_at IS NOT NULL
AND trim(old.upstream_subject)<>'' AND new.upstream_subject=old.upstream_subject
AND trim(old.issuer)<>'' AND new.issuer=old.issuer
AND trim(old.resource)<>'' AND new.resource=old.resource
AND jsonb_array_length(new.scopes)>0 AND new.permission_summary->'read_authorized'='true'::jsonb
AND new.verified_at BETWEEN :cutoff AND :now
AND (s.kind<>'CTA' OR s.remote_id IS NOT NULL)
AND (SELECT count(DISTINCT attempt.attempt_id) FROM step_evidence e
 JOIN build_attempt_context attempt ON attempt.tenant_id=e.tenant_id AND attempt.step_id=e.step_id AND attempt.attempt=e.attempt
 WHERE e.tenant_id=s.tenant_id AND e.submission_id=s.submission_id AND e.step_id=s.id
 AND e.conclusion='REQUEST_ARMED' AND e.summary->>'body_digest'=s.request_body_digest)=1
"""


def _eligible_params(session: Session, context: TenantContext) -> dict[str, Any] | None:
    try:
        _manage(session, context)
    except DomainError:
        return None
    now = datetime.now(UTC)
    return {
        "tenant": context.tenant_id,
        "now": now,
        "cutoff": now - timedelta(seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS),
    }


def historical_read_available(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    source_step_id: UUID | None = None,
) -> bool:
    params = _eligible_params(session, context)
    if params is None:
        return False
    params.update(submission=submission_id, step=source_step_id)
    return bool(
        SASession.execute(
            session,
            text(
                "SELECT EXISTS("
                + ELIGIBLE
                + " AND s.submission_id=:submission AND (CAST(:step AS uuid) IS NULL OR s.id=CAST(:step AS uuid)))"
            ),
            params,
        ).scalar_one()
    )


def historical_read_eligible_steps(
    session: Session, *, context: TenantContext, step_ids: list[UUID]
) -> set[UUID]:
    if len(step_ids) > 100:
        raise ValueError("historical read projection accepts at most 100 steps")
    params = _eligible_params(session, context)
    if params is None or not step_ids:
        return set()
    params["steps"] = step_ids
    return set(
        SASession.execute(
            session,
            text(ELIGIBLE + " AND s.id=ANY(CAST(:steps AS uuid[])) LIMIT 100"),
            params,
        ).scalars()
    )


def _verify(
    session: Session, context: TenantContext, row: BuildHistoricalRead
) -> tuple[FrozenTikTokRoute, BuildReadQuery]:
    _manage(session, context)
    if row.request_actor_id != context.actor_id:
        raise DomainError("action_forbidden", "核查请求操作者不匹配")
    if row.expires_at <= datetime.now(UTC):
        raise DomainError("historical_read_expired", "此次只读核查授权已过期")
    source, unit, old = _source(session, context, row.source_step_id)
    route = FrozenTikTokRoute.model_validate(row.new_route)
    if (
        old.model_dump(mode="json") != row.old_route
        or source.request_body_digest != row.source_request_digest
        or unit.advertiser_id != row.advertiser_id
        or original_create_attempt(session, source)
        != (row.source_attempt, row.source_attempt_id)
    ):
        raise DomainError("historical_read_source_changed", "原执行证据已改变")
    current_proof = _proof(session, context, old, route, row.advertiser_id)
    observation_times = {"old_verified_at", "new_verified_at", "grant_checked_at"}
    # 审计保留点击时的观察时间；同授权语义的新鲜再次观察不改写它，也不使只读任务失效。
    if {
        key: value
        for key, value in current_proof.items()
        if key not in observation_times
    } != {
        key: value
        for key, value in row.authorization_proof.items()
        if key not in observation_times
    }:
        raise DomainError(
            "historical_read_scope_unverified", "此次只读核查授权证据已改变"
        )
    known = _known_ids(session, source)
    if len(known) > 1 or (known and known != {row.progress.get("known_id")}):
        raise DomainError("historical_read_source_changed", "原对象身份已改变")
    return route, BuildReadQuery(
        intent=source_intent(session, source, old),
        remote_id=row.progress.get("known_id"),
        page=row.progress["page"],
    )


def _owned(row: BuildHistoricalRead, token: UUID, revision: int) -> bool:
    return (
        row.status == "RUNNING"
        and row.claim_token == token
        and row.dispatch_revision == revision
        and row.claimed_until is not None
        and row.claimed_until > datetime.now(UTC)
    )


def _finish(
    *,
    database_engine: Any,
    context: TenantContext,
    identity: UUID,
    token: UUID,
    revision: int,
    deadline: datetime,
    page: BuildPage | AdGroupStatus | None = None,
    error_code: str | None = None,
    wait: bool = False,
    call_evidence: dict[str, Any] | None = None,
) -> None:
    with (
        bounded_session(database_engine, task_deadline=deadline) as session,
        session.begin(),
    ):
        row = _row(session, context, identity, lock=True)
        if not _owned(row, token, revision):
            return
        state = "UNKNOWN"
        progress = dict(row.progress)
        observed_stage, observed_page = progress["stage"], progress["page"]
        try:
            _, query = _verify(session, context, row)
            if page is not None:

                def seen_before(ids: tuple[str, ...]) -> bool:
                    return (
                        session.exec(
                            select(BuildHistoricalReadPage.id)
                            .where(
                                BuildHistoricalReadPage.tenant_id == row.tenant_id,
                                BuildHistoricalReadPage.read_id == row.id,
                                BuildHistoricalReadPage.scan_id
                                == UUID(progress["scan_id"]),
                                BuildHistoricalReadPage.stage == "OBJECT",
                                col(BuildHistoricalReadPage.ids).op("?|")(
                                    array(list(ids))
                                ),
                            )
                            .limit(1)
                        ).first()
                        is not None
                    )

                progress, state = advance(
                    query=query, progress=progress, page=page, seen_before=seen_before
                )
        except DomainError as error:
            error_code, wait = (
                error.code
                if error.code in SAFE_ERRORS
                else "historical_read_scope_unverified",
                False,
            )
            state = "UNKNOWN"
        row.claim_token = row.claimed_until = None
        row.dispatch_id = None
        row.error_code = error_code
        row.progress = progress
        summary = {
            "outcome": state,
            "reason_code": error_code,
            "candidate": progress.get("candidate"),
        }
        session.add(
            BuildHistoricalReadPage(
                tenant_id=row.tenant_id,
                read_id=row.id,
                claim_token=token,
                scan_id=UUID(progress["scan_id"]),
                stage=observed_stage,
                page=observed_page,
                ids=[item.remote_id for item in page.rows]
                if isinstance(page, BuildPage)
                else [],
                call_evidence=asdict(page.evidence)
                if page is not None
                else call_evidence or {},
                summary=summary,
            )
        )
        if error_code is None and state == "COMPLETE":
            row.status = "CONFIRMED"
            row.remote_id = progress["candidate"]["remote_id"]
            row.mismatch = progress["candidate"]["comparison"] != "MATCH"
        elif wait or (error_code is None and state == "MORE"):
            row.status = "PENDING"
            row.dispatch_revision += 1
            _queue(session, row, delay=30 if wait else 0)
        else:
            row.status = (
                "BLOCKED"
                if error_code and error_code != "readback_response_unknown"
                else "UNKNOWN"
            )
        if row.status != "PENDING":
            row.completed_at = datetime.now(UTC)
        session.add(row)


def process_historical_read(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    payload: dict[str, Any],
) -> None:
    from app.modules.builds.dispatch import payload_identity

    require_bounded_worker()
    identity, revision = payload_identity(payload, "read_id")
    token = uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=45)
    with (
        bounded_session(database_engine, task_deadline=deadline) as session,
        session.begin(),
    ):
        row = _row(session, context, identity, lock=True)
        if row.request_actor_id != context.actor_id:
            raise DomainError("action_forbidden", "核查请求操作者不匹配")
        if (
            row.status not in {"PENDING", "RUNNING"}
            or row.dispatch_revision != revision
            or row.due_at > datetime.now(UTC)
            or row.claimed_until
            and row.claimed_until > datetime.now(UTC)
        ):
            return
        row.status, row.claim_token, row.claimed_until = "RUNNING", token, deadline
        row.repair_after = deadline + timedelta(seconds=75)
        session.add(row)
    completed = False
    try:
        with bounded_session(database_engine, task_deadline=deadline) as session:
            row = _row(session, context, identity)
            route, query = _verify(session, context, row)
            progress = dict(row.progress)

        def before_request() -> None:
            with bounded_session(database_engine, task_deadline=deadline) as session:
                current = _row(session, context, identity)
                if not _owned(current, token, revision):
                    raise DomainError(
                        "historical_read_claim_lost", "只读核查租约已失效"
                    )
                _verify(session, context, current)

        with admitted_build_call(
            redis_client, context=context, route=route, task_deadline=deadline
        ):
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                route=route,
                task_deadline=deadline,
                before_request=before_request,
            ) as gateway:
                page = (
                    gateway.builds.read_adgroup_status(
                        advertiser_id=query.intent.advertiser_id,
                        adgroup_id=progress["candidate"]["remote_id"],
                    )
                    if progress["stage"] == "STATUS"
                    else gateway.builds.read_page(query=query)
                )
                _finish(
                    database_engine=database_engine,
                    context=context,
                    identity=identity,
                    token=token,
                    revision=revision,
                    deadline=deadline,
                    page=page,
                )
                completed = True
    except Exception as error:
        if completed:
            return
        code = (
            error.code
            if isinstance(error, DomainError) and error.code in SAFE_ERRORS
            else "readback_response_unknown"
        )
        waiting = code in WAIT_ERRORS or isinstance(error, AccountAdmissionDeferred)
        _finish(
            database_engine=database_engine,
            context=context,
            identity=identity,
            token=token,
            revision=revision,
            deadline=deadline,
            error_code=code,
            wait=waiting,
            call_evidence=asdict(error.evidence)
            if isinstance(error, RemoteCallError)
            else None,
        )


def repair_historical_reads(*, database_engine: Any, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(database_engine) as session, session.begin():
        rows = session.exec(
            select(BuildHistoricalRead)
            .where(
                col(BuildHistoricalRead.status).in_(["PENDING", "RUNNING"]),
                BuildHistoricalRead.repair_after <= now,
                or_(
                    col(BuildHistoricalRead.claimed_until).is_(None),
                    col(BuildHistoricalRead.claimed_until) <= now,
                ),
            )
            .order_by(
                col(BuildHistoricalRead.repair_after), col(BuildHistoricalRead.id)
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            if row.expires_at <= now:
                row.status, row.error_code, row.completed_at = (
                    "BLOCKED",
                    "historical_read_expired",
                    now,
                )
                row.claim_token = row.claimed_until = row.dispatch_id = None
            else:
                dispatch = (
                    session.get(PendingDispatch, row.dispatch_id)
                    if row.dispatch_id
                    else None
                )
                if dispatch is None:
                    _queue(session, row)
                elif (
                    dispatch.tenant_id,
                    dispatch.actor_id,
                    dispatch.task_name,
                    dispatch.payload,
                ) != (
                    row.tenant_id,
                    row.request_actor_id,
                    TASK_NAME,
                    {"read_id": str(row.id), "revision": row.dispatch_revision},
                ):
                    row.status, row.error_code, row.completed_at = (
                        "BLOCKED",
                        "dispatch_payload_invalid",
                        now,
                    )
                    row.claim_token = row.claimed_until = None
                elif dispatch.published_at is not None:
                    dispatch.published_at, dispatch.available_at = None, now
                    session.add(dispatch)
                row.repair_after = now + timedelta(seconds=120)
            session.add(row)
        return len(rows)
