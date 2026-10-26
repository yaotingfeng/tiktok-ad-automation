"""结果未知的系列/广告组：只有核实未创建并经显式恢复批准后，才能原样重发一次。

生产 2S1T：两个系列创建时 SDK 连接层 MaxRetryError、一个广告组返回 51002，均记为
结果未知。完整回读为 0、TikTok 上确认不存在，但系统没有任何重建入口，只能人工补建。
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.modules.builds import recovery
from app.modules.builds.execution_models import ExecutionStep, StepEvidence, Submission
from app.modules.builds.execution_state import (
    approved_absent_resend,
    confirmed_absent_attempt,
)
from app.modules.builds.request_compiler import remote_request_id
from app.modules.builds.routes import save_attempt_context
from app.modules.builds.submissions import claim_step
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery import request, run


def _add(session, step, conclusion, *, at, summary, request_id=None, token=None):
    session.add(
        StepEvidence(
            tenant_id=step.tenant_id,
            submission_id=step.submission_id,
            step_id=step.id,
            attempt=step.attempt,
            lease_token=token,
            request_id=request_id,
            conclusion=conclusion,
            summary=summary,
            observed_at=at,
        )
    )


def unknown_campaign(env, *, armed_minutes_ago=15, readback=True, total=0):
    """构造与生产一致的证据链：发出 → 结果未知 → 回读递增 attempt → 完整回读。"""
    db, context, ids = env
    now = datetime.now(UTC)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        cta = session.get(ExecutionStep, ids["CTA"][0])
        cta.status, cta.phase, cta.remote_id = "SUCCEEDED", "DONE", "cta-remote"
        # 依赖与生产一致：该组合的素材都已就绪。
        for material in session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == step.submission_id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).all():
            material.status, material.phase = "SUCCEEDED", "DONE"
        step.attempt = 1
        armed_id = save_attempt_context(session, step=step)
        body = {
            "advertiser_id": "account-A",
            "campaign_name": "fixture-campaign",
            "operation_status": "ENABLE",
            "request_id": remote_request_id(armed_id),
        }
        digest = sha256(repr(sorted(body.items())).encode()).hexdigest()
        step.request_body, step.request_body_digest = body, digest
        token = uuid4()
        armed_at = now - timedelta(minutes=armed_minutes_ago)
        audit = {"attempt_id": str(armed_id), "body_digest": digest}
        _add(session, step, "REQUEST_ARMED", at=armed_at, summary=audit, token=token)
        _add(
            session,
            step,
            "RESULT_UNKNOWN",
            at=armed_at + timedelta(seconds=4),
            summary={**audit, "reason_code": "create_result_unknown"},
            token=token,
        )
        step.status, step.phase, step.error_code = (
            "UNKNOWN",
            "REQUEST_ARMED",
            "readback_inconclusive",
        )
        step.lease_token = step.lease_expires_at = None
        # 回读领取会递增本地 attempt；request_id 仍属于首次发出的 attempt。
        step.attempt = 2
        save_attempt_context(session, step=step)
        if readback:
            _add(
                session,
                step,
                "READBACK_PAGE",
                at=now,
                request_id="readback-1",
                summary={
                    "ids": [] if total == 0 else ["remote-1"],
                    "page": 1,
                    "stage": "OBJECT",
                    "total": total,
                    "result": "UNKNOWN",
                    "body_digest": digest,
                },
            )
        return step.submission_id, step.id, armed_id


def test_confirmed_absent_campaign_resends_original_request_id_once(
    executable, monkeypatch
):
    db, context, _ = executable
    submission_id, step_id, armed_id = unknown_campaign(executable)
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert confirmed_absent_attempt(session, step=step)
        assert recovery.recovery_summary(
            session, context=context, submission=session.get(Submission, submission_id)
        ).can_retry
    receipt = request(executable, submission_id)
    run(executable, receipt)
    run(executable, receipt)
    monkeypatch.setattr(
        "app.modules.builds.submissions.group_material_state", lambda *a, **k: "READY"
    )
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        assert step.status == "QUEUED" and step.phase == "IDLE"
        approval = session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step_id,
                StepEvidence.conclusion == "RETRY_REQUESTED",
            )
        ).one()
        assert approval.summary["confirmed_absent"] is True
        assert approval.summary["recovery_id"] == str(receipt.recovery_id)
        frozen = (step.request_body, step.request_body_digest)
        step.status = "PENDING"
        claim = claim_step(session, context=context, step_id=step_id, owner=uuid4())
        assert claim is not None
        # 新本地 attempt 记证据，TikTok request_id 仍是首次发出的那个。
        assert claim.attempt == 3 and claim.attempt_id != armed_id
        assert claim.remote_attempt_id == armed_id
        assert remote_request_id(claim.remote_attempt_id) == frozen[0]["request_id"]
        assert (step.request_body, step.request_body_digest) == frozen
        # 再次发出后，旧批准不能授权第二次重发。
        _add(
            session,
            step,
            "REQUEST_ARMED",
            at=datetime.now(UTC),
            summary={"attempt_id": str(claim.attempt_id), "body_digest": frozen[1]},
            token=claim.lease_token,
        )
        step.status, step.phase = "PENDING", "IDLE"
        step.lease_token = step.lease_expires_at = None
        assert not approved_absent_resend(session, step=step)
        step.status = "UNKNOWN"
        assert not confirmed_absent_attempt(session, step=step)


@pytest.mark.parametrize(
    "case", ["recent_send", "no_readback", "nonzero_readback", "created_receipt"]
)
def test_unconfirmed_unknown_campaign_never_resends(executable, case):
    db, context, _ = executable
    submission_id, step_id, _ = unknown_campaign(
        executable,
        armed_minutes_ago=3 if case == "recent_send" else 15,
        readback=case != "no_readback",
        total=1 if case == "nonzero_readback" else 0,
    )
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        if case == "created_receipt":
            _add(
                session,
                step,
                "LATE_CREATED",
                at=datetime.now(UTC),
                summary={"remote_id": "late-1"},
            )
        session.flush()
        assert not confirmed_absent_attempt(session, step=step)
        assert not recovery.recovery_summary(
            session, context=context, submission=session.get(Submission, submission_id)
        ).can_retry
