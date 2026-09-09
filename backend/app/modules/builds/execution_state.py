"""Local attempt transitions. Caller commits; no network or transport dispatch here."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.accounts.access import resolve_account_access
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.sdk_requests import RemoteCreated
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
) -> None:
    session.add(
        StepEvidence(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            step_id=step.id,
            attempt=claim.attempt if claim else step.attempt,
            lease_token=claim.lease_token if claim else step.lease_token,
            request_id=request_id,
            conclusion=conclusion,
            summary=summary or {},
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
    access = resolve_account_access(
        session,
        context=context,
        bc_id=claim.bc_id,
        advertiser_id=unit.advertiser_id,
        action="build",
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


def record_created(session: Session, *, claim: StepClaim, result: RemoteCreated) -> str:
    step = _step(session, claim)
    live = active_attempt(step, claim, phase="REQUEST_ARMED")
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="CREATED" if live else "LATE_CREATED",
        request_id=result.request_id,
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
    session: Session, *, claim: StepClaim, result: RemoteCreated
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
        request_id=result.request_id,
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
) -> str:
    step = _step(session, claim)
    evidence(
        session,
        step=step,
        claim=claim,
        conclusion="RESULT_UNKNOWN",
        request_id=request_id,
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
