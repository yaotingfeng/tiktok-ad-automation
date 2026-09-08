"""One bounded read per delivery; immutable intent, scoped leases, append-only facts.

The caller owns scheduling/repair. needs_more means schedule another read (never
create). A terminal UNKNOWN needs an explicit later reconciliation request. Page
IDs live in per-page evidence; the mutable cursor and candidate remain bounded.
"""

import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import Engine, text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred,
    admitted_account_call,
    sdk_client,
)
from app.jobs.admission import admission_policy
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.readback_sdk import (
    ENDPOINTS,
    ID_KEYS,
    NAME_KEYS,
    ReadPage,
    compare_fields,
    nonempty,
    read_page,
)
from app.modules.tenants.permissions import require_tenant

HARD_LIMIT = 45
CLAIM_SECONDS = 60
SAFE_ERRORS = frozenset(
    {
        "action_forbidden",
        "tenant_unavailable",
        "account_not_in_bc",
        "account_ownership_conflict",
        "account_metadata_incomplete",
        "account_access_denied",
        "account_authorization_changed",
        "connection_unavailable",
        "credential_invalid",
        "tiktok_app_not_configured",
        "tiktok_app_incomplete",
        "connection_encryption_unconfigured",
        "admission_unconfigured",
        "admission_policy_invalid",
        "admission_unavailable",
        "readback_response_unknown",
        "readback_intent_incomplete",
    }
)


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    needs_more: bool = False
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class _Claim:
    step_id: UUID
    source_id: UUID
    tenant_id: UUID
    submission_id: UUID
    token: UUID
    attempt: int
    revision: int
    source_attempt: int
    source_revision: int
    connection_id: UUID
    advertiser_id: str
    bc_id: str
    kind: str
    body: dict[str, Any]
    digest: str
    progress: dict[str, Any]


def require_bounded_worker() -> None:
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
        raise DomainError("reconciliation_worker_unbounded", "结果核查需要有界后台任务")


def _locked(
    session: Session, context: TenantContext, step_id: UUID
) -> tuple[ExecutionStep, ExecutionStep, BuildUnit]:
    header = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.id == step_id, ExecutionStep.tenant_id == context.tenant_id
        )
    ).one_or_none()
    if header is None:
        raise DomainError("resource_not_found", "执行步骤不存在")
    source_id = header.parent_step_id if header.kind == "READBACK" else header.id
    source = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.id == source_id, ExecutionStep.tenant_id == context.tenant_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    step = (
        source
        if source_id == step_id
        else session.exec(
            select(ExecutionStep)
            .where(
                ExecutionStep.id == step_id,
                ExecutionStep.tenant_id == context.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
    )
    if (
        source is None
        or step is None
        or source.kind not in ID_KEYS
        or source.submission_id != step.submission_id
        or source.unit_id != step.unit_id
    ):
        raise DomainError("resource_not_found", "执行步骤不存在")
    submission = session.get(Submission, step.submission_id)
    if submission is None or submission.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    unit = session.get(BuildUnit, step.unit_id)
    if (
        unit is None
        or unit.tenant_id != context.tenant_id
        or unit.preview_id != step.preview_id
    ):
        raise DomainError("resource_not_found", "执行组合不存在")
    return step, source, unit


def _authorize(
    session: Session, context: TenantContext, unit: BuildUnit, bc_id: str
) -> UUID:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=unit.advertiser_id,
        action="build",
    )
    if access.connection_id != unit.connection_id:
        raise DomainError("account_authorization_changed", "冻结账户授权已变化")
    return access.connection_id


def _known_ids(session: Session, source: ExecutionStep) -> set[str]:
    # At most two distinct receipts suffice to reject ambiguity. Never choose the
    # first late response when another attempt reports a conflicting object ID.
    values = session.exec(
        select(StepEvidence.summary["remote_id"].astext)
        .where(
            StepEvidence.tenant_id == source.tenant_id,
            StepEvidence.submission_id == source.submission_id,
            StepEvidence.step_id == source.id,
            col(StepEvidence.conclusion).in_(("CREATED", "LATE_CREATED", "RECONCILED")),
            StepEvidence.summary["remote_id"].astext.is_not(None),
        )
        .distinct()
        .limit(2)
    ).all()
    result = {value for value in values if nonempty(value)}
    if nonempty(source.remote_id):
        result.add(str(source.remote_id))
    return result


def _evidence(
    session: Session,
    step: ExecutionStep,
    *,
    conclusion: str,
    summary: dict[str, Any],
    claim: _Claim | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        StepEvidence(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            step_id=step.id,
            attempt=claim.attempt if claim else step.attempt,
            lease_token=claim.token if claim else step.lease_token,
            conclusion=conclusion,
            summary=summary,
            request_id=request_id,
        )
    )


def _unknown(step: ExecutionStep, code: str) -> None:
    if step.status != "SUCCEEDED" or not nonempty(step.remote_id):
        step.status = "UNKNOWN"
    else:
        step.mismatch = True
    step.error_code, step.updated_at = code, datetime.now(UTC)


def _claim(
    session: Session, context: TenantContext, step_id: UUID, revision: int
) -> _Claim | ReconciliationResult:
    step, source, unit = _locked(session, context, step_id)
    now = datetime.now(UTC)
    if step.dispatch_revision != revision:
        return ReconciliationResult("STALE")
    delivered = step.resolved.get("reconciliation_delivery")
    if isinstance(delivered, dict) and delivered.get("revision") == revision:
        return ReconciliationResult(
            delivered["state"],
            delivered["needs_more"],
            delivered["retry_after_seconds"],
        )
    if step.due_at > now:
        return ReconciliationResult(
            "PENDING", True, max(1, int((step.due_at - now).total_seconds()) + 1)
        )
    if any(
        row.lease_expires_at is not None and row.lease_expires_at > now
        for row in (step, source)
    ):
        return ReconciliationResult("BUSY", True, CLAIM_SECONDS)
    if step.kind == "READBACK":
        if source.status != "SUCCEEDED" or not nonempty(source.remote_id):
            return ReconciliationResult("WAITING")
    elif source.status not in {"UNKNOWN", "SUCCEEDED"}:
        return ReconciliationResult("WAITING")
    try:
        connection_id = _authorize(session, context, unit, source.bc_id)
    except DomainError as error:
        code = (
            error.code
            if error.code in SAFE_ERRORS
            else "readback_authorization_unknown"
        )
        _unknown(step, code)
        _evidence(
            session, step, conclusion="READBACK_BLOCKED", summary={"reason_code": code}
        )
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    body = source.request_body
    if not isinstance(body, dict) or body.get("advertiser_id") != unit.advertiser_id:
        _unknown(step, "readback_intent_incomplete")
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    digest = sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if digest != source.request_body_digest:
        _unknown(step, "readback_intent_incomplete")
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    known = _known_ids(session, source)
    if len(known) > 1 or source.kind == "CTA" and not known:
        _unknown(
            step, "conflicting_remote_receipt" if len(known) > 1 else "cta_unknown_id"
        )
        session.add(step)
        return ReconciliationResult("UNKNOWN")
    remote_id = next(iter(known), None)
    progress = dict(step.resolved.get("reconciliation") or {})
    if (
        progress.get("digest") != digest
        or progress.get("known_id") != remote_id
        or progress.get("done")
        or not progress.get("scan_id")
    ):
        progress = {
            "scan_id": str(uuid4()),
            "digest": digest,
            "known_id": remote_id,
            "page": 1,
            "seen": 0,
            "matches": 0,
            "stage": "OBJECT",
        }
    token = uuid4()
    step.attempt += 1
    for row in (source, step):
        row.lease_token, row.lease_expires_at = (
            token,
            now + timedelta(seconds=CLAIM_SECONDS),
        )
        row.updated_at = now
        session.add(row)
    if step.kind == "READBACK":
        step.status, step.phase = "RUNNING", "CLAIMED"
    step.resolved = {**step.resolved, "reconciliation": progress}
    session.flush()
    return _Claim(
        step.id,
        source.id,
        step.tenant_id,
        step.submission_id,
        token,
        step.attempt,
        revision,
        source.attempt,
        source.dispatch_revision,
        connection_id,
        unit.advertiser_id,
        source.bc_id,
        source.kind,
        dict(body),
        digest,
        progress,
    )


def _active(step: ExecutionStep, source: ExecutionStep, claim: _Claim) -> bool:
    now = datetime.now(UTC)
    return (
        all(
            row.lease_token == claim.token
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
            for row in (step, source)
        )
        and step.attempt == claim.attempt
        and step.dispatch_revision == claim.revision
        and source.attempt == claim.source_attempt
        and source.dispatch_revision == claim.source_revision
    )


def _clear(step: ExecutionStep, source: ExecutionStep) -> None:
    for row in (source, step):
        row.lease_token = row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)


def _safe_status(row: dict[str, Any], field: str) -> str | None:
    value = row.get(field)
    return (
        value
        if isinstance(value, str)
        and 0 < len(value) <= 128
        and all(ch.isalnum() or ch == "_" for ch in value)
        else None
    )


def _candidate(claim: _Claim, row: dict[str, Any]) -> dict[str, Any] | None:
    identity = row.get(ID_KEYS[claim.kind])
    if not nonempty(identity) or len(identity) > 128:
        raise DomainError("readback_response_unknown", "对象身份不完整")
    if claim.kind != "CTA":
        for field in (
            "advertiser_id",
            *({"ADGROUP": ("campaign_id",), "AD": ("adgroup_id",)}.get(claim.kind, ())),
        ):
            if row.get(field) != claim.body.get(field):
                raise DomainError("readback_response_unknown", "返回对象范围不匹配")
        name_key = NAME_KEYS[claim.kind]
        if not nonempty(row.get(name_key)):
            raise DomainError("readback_response_unknown", "返回对象名称不完整")
        if claim.progress.get("known_id"):
            if identity != claim.progress["known_id"]:
                raise DomainError("readback_response_unknown", "返回对象标识不匹配")
        elif row[name_key] != claim.body.get(name_key):
            return None
    elif identity != claim.progress.get("known_id"):
        raise DomainError("readback_response_unknown", "返回对象标识不匹配")
    return {
        "remote_id": identity,
        "comparison": compare_fields(claim.kind, claim.body, row),
        "operation_status": _safe_status(row, "operation_status"),
        "review_status": _safe_status(row, "secondary_status"),
    }


def _advance(
    session: Session, claim: _Claim, page: ReadPage
) -> tuple[dict[str, Any], str]:
    progress = dict(claim.progress)
    if progress["stage"] == "STATUS":
        # A fresh standard GET may never confirm an object from a different scope.
        if len(page.rows) != 1 or page.total_number != 1:
            return {**progress, "done": True}, "UNKNOWN"
        row, candidate = page.rows[0], dict(progress["candidate"])
        for key in ("advertiser_id", "campaign_id", "adgroup_name"):
            if row.get(key) != claim.body.get(key):
                return {**progress, "done": True}, "UNKNOWN"
        if row.get("adgroup_id") != candidate["remote_id"]:
            return {**progress, "done": True}, "UNKNOWN"
        core: dict[str, Any] = {
            key: claim.body[key]
            for key in ("advertiser_id", "campaign_id", "adgroup_name", "roas_bid")
            if key in claim.body
        }
        comparison = compare_fields("ADGROUP", core, row)
        if (
            comparison == "INCOMPLETE"
            or comparison != "MATCH"
            and not progress.get("known_id")
        ):
            return {**progress, "done": True}, "UNKNOWN"
        if comparison != "MATCH":
            candidate["comparison"] = comparison
        candidate["operation_status"] = _safe_status(row, "operation_status")
        candidate["review_status"] = _safe_status(row, "secondary_status")
        return {**progress, "candidate": candidate, "done": True}, "COMPLETE"
    ids = [row.get(ID_KEYS[claim.kind]) for row in page.rows]
    if any(not nonempty(identity) or len(identity) > 128 for identity in ids) or len(
        set(ids)
    ) != len(ids):
        return {**progress, "done": True}, "UNKNOWN"
    if page.page > 1:
        duplicate = session.execute(  # ty: ignore[deprecated] -- bounded PostgreSQL evidence query
            text("""
            SELECT EXISTS(SELECT 1 FROM step_evidence
            WHERE tenant_id=:tenant AND submission_id=:submission AND step_id=:step
              AND conclusion='READBACK_PAGE' AND summary->>'scan_id'=:scan
              AND (summary->'ids') ?| CAST(:ids AS text[]))
        """),
            {
                "tenant": claim.tenant_id,
                "submission": claim.submission_id,
                "step": claim.step_id,
                "scan": progress["scan_id"],
                "ids": ids,
            },
        ).scalar_one()
        if (
            duplicate
            or progress.get("total") != page.total_number
            or progress.get("pages") != page.total_pages
        ):
            return {**progress, "done": True}, "UNKNOWN"
    progress.update(
        total=page.total_number,
        pages=page.total_pages,
        seen=progress["seen"] + len(ids),
    )
    for row in page.rows:
        found = _candidate(claim, row)
        if found is not None:
            progress["matches"] = min(2, progress["matches"] + 1)
            if progress["matches"] == 1:
                progress["candidate"] = found
    if page.page < page.total_pages:
        progress["page"] = page.page + 1
        return progress, "MORE"
    progress["done"] = True
    if progress["seen"] != page.total_number or progress["matches"] != 1:
        return progress, "UNKNOWN"
    candidate = progress["candidate"]
    if (
        candidate["comparison"] == "INCOMPLETE"
        or candidate["comparison"] != "MATCH"
        and not progress.get("known_id")
    ):
        return progress, "UNKNOWN"
    if claim.kind == "ADGROUP" and candidate["operation_status"] is None:
        progress.update(stage="STATUS", page=1, done=False)
        return progress, "MORE"
    return progress, "COMPLETE"


def _remember(
    step: ExecutionStep, result: ReconciliationResult
) -> ReconciliationResult:
    step.resolved = {
        **step.resolved,
        "reconciliation_delivery": {
            "revision": step.dispatch_revision,
            "state": result.state,
            "needs_more": result.needs_more,
            "retry_after_seconds": result.retry_after_seconds,
        },
    }
    return result


def _finish(
    database_engine: Engine,
    context: TenantContext,
    claim: _Claim,
    page: ReadPage | None,
    *,
    error: str | None = None,
    delay: int = 0,
) -> ReconciliationResult:
    with Session(database_engine) as session, session.begin():
        step, source, _ = _locked(session, context, claim.step_id)
        live = _active(step, source, claim)
        summary: dict[str, Any] = {
            "scan_id": claim.progress["scan_id"],
            "page": claim.progress["page"],
            "stage": claim.progress["stage"],
            "body_digest": claim.digest,
        }
        if page:
            summary.update(
                ids=[
                    row.get(ID_KEYS[claim.kind])
                    for row in page.rows
                    if nonempty(row.get(ID_KEYS[claim.kind]))
                    and len(row[ID_KEYS[claim.kind]]) <= 128
                ][:100],
                total=page.total_number,
            )
        if error:
            summary["reason_code"] = error
        if not live:
            _evidence(
                session,
                step,
                claim=claim,
                conclusion="LATE_READBACK",
                summary=summary,
                request_id=page.request_id if page else None,
            )
            return ReconciliationResult("STALE")
        if error or page is None:
            _evidence(
                session, step, claim=claim, conclusion="READBACK_ERROR", summary=summary
            )
            _unknown(step, error or "readback_response_unknown")
            _clear(step, source)
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("UNKNOWN", delay > 0, delay))
        known = _known_ids(session, source)
        try:
            progress, result = _advance(session, claim, page)
        except DomainError:
            progress, result = {**claim.progress, "done": True}, "UNKNOWN"
        if len(known) > 1 or known and known != {claim.progress.get("known_id")}:
            progress, result = {**progress, "done": True}, "UNKNOWN"
        summary.update(candidate=progress.get("candidate"), result=result)
        _evidence(
            session,
            step,
            claim=claim,
            conclusion="READBACK_PAGE",
            summary=summary,
            request_id=page.request_id,
        )
        step.resolved = {**step.resolved, "reconciliation": progress}
        _clear(step, source)
        if result == "MORE":
            _unknown(step, "readback_in_progress")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("PENDING", True, 0))
        candidate_value = progress.get("candidate")
        candidate: dict[str, Any] = (
            candidate_value if isinstance(candidate_value, dict) else {}
        )
        status = candidate.get("operation_status")
        if (
            result != "COMPLETE"
            or claim.kind != "CTA"
            and status not in {"ENABLE", "DISABLE", "FROZEN"}
        ):
            _unknown(step, "readback_inconclusive")
            session.add_all([step, source])
            return _remember(step, ReconciliationResult("UNKNOWN"))
        mismatch = (
            candidate["comparison"] != "MATCH"
            or claim.kind != "CTA"
            and status != "ENABLE"
        )
        now = datetime.now(UTC)
        for row in (source, step):
            row.remote_id, row.status, row.phase = (
                candidate["remote_id"],
                "SUCCEEDED",
                "DONE",
            )
            row.mismatch, row.checked_at = mismatch, now
            row.operation_status, row.review_status = (
                status,
                candidate.get("review_status"),
            )
            row.error_code = "readback_mismatch" if mismatch else None
            session.add(row)
        # Receipt on the create step allows later independent READBACK delivery to
        # reuse the actual ID. The saved create request is never altered.
        _evidence(
            session,
            source,
            claim=claim,
            conclusion="RECONCILED",
            summary={"remote_id": candidate["remote_id"], "mismatch": mismatch},
            request_id=page.request_id,
        )
        return _remember(
            step, ReconciliationResult("MISMATCH" if mismatch else "SUCCEEDED")
        )


def process_reconciliation(
    *,
    database_engine: Engine,
    redis_client: Redis,
    context: TenantContext,
    step_id: UUID,
    revision: int,
) -> ReconciliationResult:
    require_bounded_worker()
    with Session(database_engine) as session, session.begin():
        claimed = _claim(session, context, step_id, revision)
    if isinstance(claimed, ReconciliationResult):
        return claimed
    claim = claimed
    status_only = claim.progress["stage"] == "STATUS"
    endpoint = ENDPOINTS["ADGROUP_STATUS" if status_only else claim.kind]
    try:
        policy = admission_policy(endpoint)
        if policy.lease_ms <= (HARD_LIMIT + 5) * 1000:
            raise DomainError(
                "admission_policy_invalid", "调用租约必须覆盖硬期限和清理"
            )
        with (
            admitted_account_call(
                redis_client,
                context=context,
                endpoint=endpoint,
                advertiser_id=claim.advertiser_id,
                policy=policy,
            ),
            ExitStack() as stack,
        ):
            with Session(database_engine) as session, session.begin():
                step, source, unit = _locked(session, context, step_id)
                if not _active(step, source, claim):
                    return ReconciliationResult("STALE")
                connection_id = _authorize(session, context, unit, claim.bc_id)
                if connection_id != claim.connection_id:
                    raise DomainError(
                        "account_authorization_changed", "冻结账户授权已变化"
                    )
                client = stack.enter_context(
                    sdk_client(session, context=context, connection_id=connection_id)
                )
            # No database transaction/connection survives the actual request or
            # SDK thread/client cleanup; the Redis lease covers both.
            page = read_page(
                client,
                kind=claim.kind,
                body=claim.body,
                remote_id=claim.progress["candidate"]["remote_id"]
                if status_only
                else claim.progress.get("known_id"),
                page=claim.progress["page"],
                status_only=status_only,
            )
    except AccountAdmissionDeferred as error:
        return _finish(
            database_engine,
            context,
            claim,
            None,
            error="admission_deferred",
            delay=max(1, (error.retry_after_ms + 999) // 1000),
        )
    except DomainError as error:
        code = error.code if error.code in SAFE_ERRORS else "readback_response_unknown"
        return _finish(
            database_engine,
            context,
            claim,
            None,
            error=code,
            delay=30
            if code in {"admission_unavailable", "readback_response_unknown"}
            else 0,
        )
    return _finish(database_engine, context, claim, page)
