"""Local attempt transitions. Caller commits; no network or transport dispatch here."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.adapters.build_results import safe_identifier
from app.integrations.tiktok.contracts.builds import CreatedObject
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import save_attempt_context, verify_unit_route
from app.modules.tenants.permissions import require_tenant


def _step(session: Session, claim: StepClaim) -> ExecutionStep:
    step = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.id == claim.step_id,
            ExecutionStep.tenant_id == claim.tenant_id,
            ExecutionStep.submission_id == claim.submission_id,
            ExecutionStep.unit_id == claim.unit_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if step is None or step.preview_id != claim.preview_id or step.kind != claim.kind:
        raise DomainError("resource_not_found", "执行步骤不存在")
    submission = session.get(Submission, step.submission_id, populate_existing=True)
    if submission is None or submission.actor_id != claim.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    return step


def active_attempt(step: ExecutionStep, claim: StepClaim, *, phase: str) -> bool:
    return (
        step.status == "RUNNING"
        and step.phase == phase
        and step.lease_token == claim.lease_token
        and step.attempt == claim.attempt
        and step.attempt_id == claim.attempt_id
        and step.lease_expires_at is not None
        and step.lease_expires_at > datetime.now(UTC)
        and step.dispatch_revision == claim.dispatch_revision
    )


def evidence(
    session: Session,
    *,
    step: ExecutionStep,
    claim: StepClaim | None,
    conclusion: str,
    request_id: str | None = None,
    summary: dict[str, Any] | None = None,
    call_evidence: CallEvidence | None = None,
) -> None:
    save_attempt_context(
        session,
        step=step,
        attempt=claim.attempt if claim else step.attempt,
        expected_id=claim.attempt_id if claim else None,
    )
    safe_summary = dict(summary or {})
    if claim is not None:
        safe_summary["attempt_id"] = str(claim.attempt_id)
    if call_evidence is not None:
        for name in ("mcp_request_id", "remote_task_id"):
            value = safe_identifier(getattr(call_evidence, name))
            if value is not None:
                safe_summary[name] = value
    session.add(
        StepEvidence(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            step_id=step.id,
            attempt=claim.attempt if claim else step.attempt,
            lease_token=claim.lease_token if claim else step.lease_token,
            request_id=request_id,
            conclusion=conclusion,
            summary=safe_summary,
        )
    )


def arm_request(
    session: Session, *, context: TenantContext, claim: StepClaim, body: dict[str, Any]
) -> str:
    if (context.tenant_id, context.actor_id) != (claim.tenant_id, claim.actor_id):
        raise DomainError("action_forbidden", "执行范围不匹配")
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    step = _step(session, claim)
    if not active_attempt(step, claim, phase="CLAIMED") or step.remote_id:
        raise DomainError("execution_lease_lost", "执行租约已变化")
    unit = session.get(BuildUnit, step.unit_id, populate_existing=True)
    if unit is None or unit.tenant_id != context.tenant_id:
        raise DomainError("resource_not_found", "执行组合不存在")
    route = verify_unit_route(session, context=context, unit=unit, capability="build")
    if route != claim.route:
        raise DomainError("frozen_route_changed", "执行尝试的父路线不一致")
    access = resolve_account_access(
        session,
        context=context,
        bc_id=claim.bc_id,
        advertiser_id=unit.advertiser_id,
        action="build",
        connection_id=route.connection_id,
    )
    if (
        access.connection_id != unit.connection_id
        or access.advertiser_id != claim.advertiser_id
    ):
        raise DomainError("account_authorization_changed", "冻结账户授权已变化")
    if not isinstance(body, dict) or body.get("advertiser_id") != claim.advertiser_id:
        raise DomainError("invalid_build_request", "请求账户与冻结组合不一致")
    if claim.kind == "AD":
        from app.modules.builds.cover_execution import validate_ad_assets

        validate_ad_assets(session, step=step, unit=unit, body=body)
    if (
        claim.kind in {"CAMPAIGN", "ADGROUP", "AD"}
        and body.get("operation_status") != "ENABLE"
    ):
        raise DomainError("invalid_creation_status", "创建状态必须为启用")
    try:
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except TypeError, ValueError, RecursionError:
        raise DomainError("invalid_build_request", "搭建请求格式无效") from None
    if len(encoded.encode()) > 262144:
        raise DomainError("invalid_build_request", "单步请求超出支持范围")
    digest = sha256(encoded.encode()).hexdigest()
    if step.request_body is not None and step.request_body_digest != digest:
        raise DomainError("execution_intent_changed", "已保存的执行请求不能改变")
    step.request_body, step.request_body_digest = json.loads(encoded), digest
    step.phase, step.updated_at = "REQUEST_ARMED", datetime.now(UTC)
    session.add(step)
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="REQUEST_ARMED",
        summary={"body_digest": digest},
    )
    session.flush()
    return digest


def record_created(session: Session, *, claim: StepClaim, result: CreatedObject) -> str:
    step = _step(session, claim)
    live = active_attempt(step, claim, phase="REQUEST_ARMED")
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="CREATED" if live else "LATE_CREATED",
        request_id=result.evidence.request_id,
        call_evidence=result.evidence,
        summary={
            "remote_id": result.remote_id,
            "operation_status": result.operation_status,
        },
    )
    if live:
        step.remote_id, step.status, step.phase = result.remote_id, "SUCCEEDED", "DONE"
        step.operation_status, step.error_code = result.operation_status, None
        step.lease_token = step.lease_expires_at = None
    elif step.status != "SUCCEEDED" and not step.remote_id:
        # Stop any replacement owner: the old attempt now has possible effect.
        # Reconciliation alone may choose the real object from this evidence.
        step.status, step.error_code = "UNKNOWN", "late_creation_receipt"
    elif step.remote_id != result.remote_id:
        step.mismatch, step.error_code = True, "conflicting_remote_receipt"
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.flush()
    return step.status


def preserve_created_receipt(
    session: Session, *, claim: StepClaim, result: CreatedObject
) -> str:
    """Persist a received ID after a failed success transaction, without a replay.

    This fresh transaction writes append-only evidence. A previously committed
    success remains successful; an uncommitted receipt needs readback. Never
    change a newer nonce, attempt, expiry, phase, body, or actual object ID.
    """
    step = _step(session, claim)
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="LATE_CREATED",
        request_id=result.evidence.request_id,
        call_evidence=result.evidence,
        summary={
            "remote_id": result.remote_id,
            "operation_status": result.operation_status,
        },
    )
    if step.remote_id:
        if step.remote_id != result.remote_id:
            step.mismatch, step.error_code = True, "conflicting_remote_receipt"
    elif step.status != "SUCCEEDED":
        step.status, step.error_code = "UNKNOWN", "late_creation_receipt"
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.flush()
    return step.status


def record_unknown(
    session: Session,
    *,
    claim: StepClaim,
    code: str,
    request_id: str | None = None,
    remote_code: int | None = None,
    call_evidence: CallEvidence | None = None,
) -> str:
    step = _step(session, claim)
    # 保存已验证的原业务码用于诊断；错误码本身不证明无副作用，状态仍为 UNKNOWN。
    if remote_code is None and call_evidence is not None:
        remote_code = call_evidence.remote_code
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="RESULT_UNKNOWN",
        request_id=request_id,
        call_evidence=call_evidence,
        summary={"reason_code": code, "remote_code": remote_code},
    )
    if step.status != "SUCCEEDED" and not step.remote_id:
        step.status, step.error_code = "UNKNOWN", code
        step.updated_at = datetime.now(UTC)
        session.add(step)
    session.flush()
    return step.status


def finish_local(
    session: Session,
    *,
    claim: StepClaim,
    status: str,
    code: str | None = None,
    resolved: dict[str, Any] | None = None,
    delay: int = 0,
) -> str:
    """Pre-network failure, a queued material dependency, or a verified mapping."""
    step = _step(session, claim)
    if status not in {"FAILED", "PENDING", "SUCCEEDED"} and not (
        status == "UNKNOWN" and claim.kind == "MATERIAL" and step.cover_job_id
    ):
        raise ValueError("unsupported local outcome")
    if not active_attempt(step, claim, phase="CLAIMED"):
        return step.status
    step.status, step.phase, step.error_code = (
        status,
        "IDLE" if status == "PENDING" else "DONE",
        code,
    )
    if resolved is not None:
        step.resolved = resolved
    step.lease_token = step.lease_expires_at = None
    step.due_at = datetime.now(UTC) + timedelta(seconds=max(0, delay))
    step.updated_at = datetime.now(UTC)
    session.add(step)
    if status != "PENDING":
        evidence(
            session,
            step=step,
            claim=claim,
            conclusion="LOCAL_" + status,
            summary={"reason_code": code},
        )
    session.flush()
    return status


def expire_attempt(session: Session, *, step: ExecutionStep) -> str:
    """Caller owns the row lock. Time passing is never proof of remote absence."""
    if step.status == "SUCCEEDED" or step.remote_id:
        return "READBACK"
    if step.status == "UNKNOWN":
        return "RECONCILE"
    if not step.lease_expires_at or step.lease_expires_at > datetime.now(UTC):
        return "WAIT"
    if step.phase == "REQUEST_ARMED" or step.request_body is not None:
        step.status, step.error_code = "UNKNOWN", "worker_result_unknown"
        evidence(session, step=step, claim=None, conclusion="LEASE_EXPIRED_ARMED")
        action = "RECONCILE"
    else:
        step.status, step.phase = "PENDING", "IDLE"
        step.lease_token = step.lease_expires_at = None
        action = "RECLAIM"
    step.updated_at = datetime.now(UTC)
    session.add(step)
    return action


def record_not_sent(
    session: Session,
    *,
    claim: StepClaim,
    error: RemoteCallError,
    retryable: bool,
    delay: int = 15,
) -> str:
    """只有传输明确未发送才能退回待调度；保留原不可变正文与 attempt。"""
    if error.effect != "NOT_SENT":
        raise ValueError("only NOT_SENT can be locally rescheduled")
    step = _step(session, claim)
    if not active_attempt(step, claim, phase="REQUEST_ARMED"):
        return step.status
    retryable, delay, step.resolved = transient_retry(
        step.resolved, error=error, retryable=retryable, delay=delay
    )
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="NOT_SENT",
        request_id=error.evidence.request_id,
        call_evidence=error.evidence,
        summary={"reason_code": error.code, "body_digest": step.request_body_digest},
    )
    step.status, step.phase = ("PENDING", "IDLE") if retryable else ("FAILED", "DONE")
    step.error_code = error.code
    # 当前 nonce 留在不可变 NOT_SENT 证据中；新 owner 必须验证最后一个尝试结果。
    step.lease_token = step.lease_expires_at = None
    step.due_at = datetime.now(UTC) + timedelta(seconds=max(0, delay))
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.flush()
    return step.status


_BUSINESS_REJECTION_RETRIES = {40002: 1, 51002: 2}


def record_rejected(
    session: Session,
    *,
    claim: StepClaim,
    error: RemoteCallError,
) -> str:
    """持久化明确无副作用的远端拒绝，并按业务码执行有限重试。"""
    if error.effect != "REJECTED_NO_EFFECT":
        raise ValueError("only REJECTED_NO_EFFECT can use the rejection retry budget")
    remote_code = error.evidence.remote_code
    if remote_code not in _BUSINESS_REJECTION_RETRIES:
        raise ValueError("business rejection code is not retryable")
    step = _step(session, claim)
    if not active_attempt(step, claim, phase="REQUEST_ARMED"):
        return step.status
    raw_counts = step.resolved.get("business_rejection_counts", {})
    counts = dict(raw_counts) if isinstance(raw_counts, dict) else {}
    key = str(remote_code)
    previous = counts.get(key, 0)
    count = previous + 1 if type(previous) is int and previous >= 0 else 1
    counts[key] = count
    retryable = count <= _BUSINESS_REJECTION_RETRIES[remote_code]
    delay = min(30, 5 * 2 ** (count - 1))
    step.resolved = {**step.resolved, "business_rejection_counts": counts}
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="REMOTE_REJECTED",
        request_id=error.evidence.request_id,
        call_evidence=error.evidence,
        summary={
            "reason_code": error.code,
            "remote_code": remote_code,
            "body_digest": step.request_body_digest,
            "rejection_count": count,
        },
    )
    step.status, step.phase = ("PENDING", "IDLE") if retryable else ("FAILED", "DONE")
    step.error_code = error.code
    step.lease_token = step.lease_expires_at = None
    step.due_at = datetime.now(UTC) + timedelta(seconds=delay)
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.flush()
    return step.status


def transient_retry(
    resolved: dict[str, Any],
    *,
    error: DomainError,
    retryable: bool,
    delay: int,
    definitely_not_sent: bool = False,
) -> tuple[bool, int, dict[str, Any]]:
    """未发送传输异常最多尝试三次；同一 attempt 的多个 nonce 共用计数。"""
    from app.integrations.tiktok.contracts.common import TRANSIENT_NOT_SENT

    if (
        definitely_not_sent
        or isinstance(error, RemoteCallError)
        and error.effect == "NOT_SENT"
    ) and error.code in TRANSIENT_NOT_SENT:
        failures = int(resolved.get("transport_failure_count", 0)) + 1
        return (
            failures < 3,
            min(60, 5 * 2 ** min(failures - 1, 4)),
            {**resolved, "transport_failure_count": failures},
        )
    return retryable, delay, resolved


# 候选统计、恢复持锁复查与 worker claim 共用这一证明，避免 SQL/Python 放行条件漂移。
# 每个 nonce 必须严格只有 ARM -> NOT_SENT；恢复审计只允许引用本提交的真实 RETRY。
UNSENT_ATTEMPT = """(
s.kind IN ('CTA','CAMPAIGN','ADGROUP','AD')
AND s.remote_id IS NULL AND NOT s.mismatch AND s.lease_token IS NULL
AND s.request_body IS NOT NULL AND s.request_body <> '{}'::jsonb
AND s.request_body_digest IS NOT NULL AND s.request_body_digest <> ''
AND s.attempt_id IS NOT NULL AND s.attempt > 0
AND NOT EXISTS (SELECT 1 FROM step_evidence danger
 WHERE danger.tenant_id=s.tenant_id AND danger.submission_id=s.submission_id AND danger.step_id=s.id
 AND (danger.summary ? 'remote_id' OR danger.conclusion IN ('CREATED','LATE_CREATED','RESULT_UNKNOWN','LEASE_EXPIRED_ARMED')
 OR (danger.attempt<>s.attempt AND danger.conclusion IN ('REQUEST_ARMED','NOT_SENT'))))
AND (WITH proof AS (
 SELECT e.* FROM step_evidence e
 WHERE e.tenant_id=s.tenant_id AND e.submission_id=s.submission_id AND e.step_id=s.id AND e.attempt=s.attempt
 LIMIT 1001
), nonces AS (
 SELECT count(*) n, array_agg(conclusion ORDER BY observed_at,id) phases
 FROM proof WHERE conclusion<>'RETRY_REQUESTED' GROUP BY lease_token
)
SELECT (SELECT count(*) BETWEEN 1 AND 1000 FROM proof)
 AND (SELECT coalesce(bool_and(n=2 AND phases=ARRAY['REQUEST_ARMED','NOT_SENT']::varchar[]),false) FROM nonces)
 AND (SELECT coalesce(bool_and(coalesce(
   summary->>'attempt_id'=CAST(s.attempt_id AS text)
   AND summary->>'body_digest'=s.request_body_digest
   AND ((conclusion IN ('REQUEST_ARMED','NOT_SENT') AND lease_token IS NOT NULL)
     OR (conclusion='RETRY_REQUESTED' AND lease_token IS NULL AND EXISTS (
       SELECT 1 FROM submission_recovery r WHERE r.tenant_id=s.tenant_id
       AND r.submission_id=s.submission_id AND r.kind='RETRY'
       AND CAST(r.id AS text)=proof.summary->>'recovery_id'))),false)),false) FROM proof)
))"""

# 自动重试可以混合“明确未发送”和“明确拒绝且无副作用”的 nonce；仍要求每个
# nonce 都有严格的 ARM -> terminal 两条证据，任何未知结果或创建回执都会封死重放。
AUTOMATIC_RETRY_ATTEMPT = (
    UNSENT_ATTEMPT.replace(
        "danger.conclusion IN ('REQUEST_ARMED','NOT_SENT')",
        "danger.conclusion IN ('REQUEST_ARMED','NOT_SENT','REMOTE_REJECTED')",
    )
    .replace(
        "phases=ARRAY['REQUEST_ARMED','NOT_SENT']::varchar[]",
        "phases IN (ARRAY['REQUEST_ARMED','NOT_SENT']::varchar[], ARRAY['REQUEST_ARMED','REMOTE_REJECTED']::varchar[])",
    )
    .replace(
        "conclusion IN ('REQUEST_ARMED','NOT_SENT')",
        "conclusion IN ('REQUEST_ARMED','NOT_SENT','REMOTE_REJECTED')",
    )
)


def safely_unsent_attempt(
    session: Session, *, step: ExecutionStep, recovery: bool = False
) -> bool:
    """复用正文前核对逐 nonce 完整证明；只有人工恢复可接纳终止状态。"""
    if (
        (step.status not in {"FAILED", "RETRYABLE"} or step.phase != "DONE")
        if recovery
        else (step.status not in {"PENDING", "QUEUED"} or step.phase != "IDLE")
    ):
        return False
    session.flush()
    return bool(
        SASession.execute(
            session,
            text(
                "SELECT EXISTS(SELECT 1 FROM execution_step s WHERE s.id=:step AND s.tenant_id=:tenant AND "
                + UNSENT_ATTEMPT
                + ")"
            ),
            {"step": step.id, "tenant": step.tenant_id},
        ).scalar_one()
    )


def safely_retryable_attempt(session: Session, *, step: ExecutionStep) -> bool:
    """核对自动重试的完整逐 nonce 证据，不扩大人工恢复边界。"""
    if step.status not in {"PENDING", "QUEUED"} or step.phase != "IDLE":
        return False
    session.flush()
    return bool(
        SASession.execute(
            session,
            text(
                "SELECT EXISTS(SELECT 1 FROM execution_step s WHERE s.id=:step AND s.tenant_id=:tenant AND "
                + AUTOMATIC_RETRY_ATTEMPT
                + ")"
            ),
            {"step": step.id, "tenant": step.tenant_id},
        ).scalar_one()
    )
