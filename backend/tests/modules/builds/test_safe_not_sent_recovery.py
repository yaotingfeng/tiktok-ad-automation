"""真实持久恢复服务保留未发送正文，后继只在实际依赖恢复后继续。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import httpx2
import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.modules.builds import recovery
from app.modules.builds.dispatch import process_unit, wake_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    StepEvidence,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_state import (
    arm_request,
    evidence,
    record_not_sent,
    safely_unsent_attempt,
)
from app.modules.builds.routes import save_attempt_context
from app.modules.builds.submissions import claim_step
from tests.modules.builds.test_channel_execution import (
    app_config as app_config,
)
from tests.modules.builds.test_channel_execution import (
    channel_execution as channel_execution,
)
from tests.modules.builds.test_channel_execution import (
    created,
)
from tests.modules.builds.test_channel_execution import (
    database_engine as database_engine,
)
from tests.modules.builds.test_channel_execution import (
    gateway_case as gateway_case,
)
from tests.modules.builds.test_channel_execution import (
    gateway_wire as gateway_wire,
)
from tests.modules.builds.test_channel_execution import (
    policy as policy,
)
from tests.modules.builds.test_channel_execution import (
    retain_build_history as retain_build_history,
)
from tests.modules.builds.test_channel_execution import (
    scene_case as scene_case,
)
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery import request, run


def failed_unsent(env):
    db, context, ids = env
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["CTA"][0])
        claim = claim_step(session, context=context, step_id=step.id, owner=uuid4())
        arm_request(
            session, context=context, claim=claim, body={"advertiser_id": "account-A"}
        )
        record_not_sent(
            session,
            claim=claim,
            error=RemoteCallError(
                "mcp_catalog_unavailable", effect="NOT_SENT", evidence=CallEvidence()
            ),
            retryable=False,
            delay=0,
        )
        assert step.status == "FAILED"
        step.resolved = {**step.resolved, "transport_failure_count": 3}
        session.add(step)
        return step.submission_id, step.id, claim


def tick(env, unit_id):
    db, context, _ = env
    with Session(db) as session, session.begin():
        wake_unit(session, context=context, unit_id=unit_id)
        unit = session.exec(
            select(SubmissionUnit).where(SubmissionUnit.unit_id == unit_id)
        ).one()
        revision = unit.dispatch_revision
    return process_unit(
        database_engine=db,
        context=context,
        payload={"unit_id": str(unit_id), "revision": revision},
    )


def test_manual_retry_preserves_frozen_attempt_and_accepts_its_audit(executable):
    db, context, _ = executable
    submission_id, step_id, old = failed_unsent(executable)
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        frozen = (
            step.request_body,
            step.request_body_digest,
            step.attempt,
            step.attempt_id,
        )
        assert recovery.recovery_summary(
            session, context=context, submission=session.get(Submission, submission_id)
        ).can_retry
    receipt = request(executable, submission_id)
    run(executable, receipt)
    run(executable, receipt)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        assert step.status == "QUEUED"
        assert (
            step.request_body,
            step.request_body_digest,
            step.attempt,
            step.attempt_id,
        ) == frozen
        assert step.resolved["transport_failure_count"] == 0
        audit = session.exec(
            select(StepEvidence).where(
                StepEvidence.step_id == step_id,
                StepEvidence.conclusion == "RETRY_REQUESTED",
            )
        ).one()
        assert audit.summary["recovery_id"] == str(receipt.recovery_id)
        assert audit.summary["body_digest"] == frozen[1]
        assert audit.summary["transport_failure_count"] == 3
        claim = claim_step(session, context=context, step_id=step.id, owner=uuid4())
        assert claim.attempt_id == old.attempt_id and claim.route == old.route
        assert claim.lease_token != old.lease_token
        assert (
            arm_request(session, context=context, claim=claim, body=frozen[0])
            == frozen[1]
        )


@pytest.mark.parametrize(
    "conclusion", ["RESULT_UNKNOWN", "LATE_CREATED", "CREATED", "REQUEST_ARMED"]
)
def test_manual_retry_rejects_incomplete_or_effectful_nonce_evidence(
    executable, conclusion
):
    db, _, _ = executable
    submission_id, step_id, claim = failed_unsent(executable)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        evidence(
            session,
            step=step,
            claim=claim.model_copy(update={"lease_token": uuid4()}),
            conclusion=conclusion,
            summary={"body_digest": step.request_body_digest},
        )
    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request(executable, submission_id)


def test_dependency_failed_chain_wakes_after_predecessor_success(executable):
    db, _, ids = executable
    with Session(db) as session, session.begin():
        cta = session.get(ExecutionStep, ids["CTA"][0])
        cta.status, cta.phase, cta.remote_id = "SUCCEEDED", "DONE", "existing-cta"
        unit_id = cta.unit_id
        for identity in ids["MATERIAL"]:
            session.get(ExecutionStep, identity).status = "SUCCEEDED"
        for kind in ("CAMPAIGN", "ADGROUP", "AD"):
            for identity in ids[kind]:
                step = session.get(ExecutionStep, identity)
                step.status, step.phase, step.error_code = (
                    "FAILED",
                    "DONE",
                    "dependency_failed",
                )
    for kind in ("CAMPAIGN", "ADGROUP", "AD"):
        tick(executable, unit_id)
        with Session(db) as session, session.begin():
            for identity in ids[kind]:
                step = session.get(ExecutionStep, identity)
                assert step.status == "QUEUED", (kind, step.status)
                step.status, step.phase, step.remote_id = (
                    "SUCCEEDED",
                    "DONE",
                    "actual-" + str(identity),
                )
                step.dispatch_id = None
    tick(executable, unit_id)
    with Session(db) as session:
        assert not ids["READBACK"]
        assert session.get(ExecutionStep, ids["CTA"][0]).remote_id == "existing-cta"


def test_two_frozen_retry_jobs_schedule_one_dispatch_and_one_reset(executable):
    db, _, _ = executable
    submission_id, step_id, _ = failed_unsent(executable)
    receipts = [request(executable, submission_id), request(executable, submission_id)]
    barrier = Barrier(2)

    def recover(receipt):
        barrier.wait(timeout=10)
        run(executable, receipt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(recover, receipts))
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert step.dispatch_revision == 1 and step.status == "QUEUED"
        assert (
            len(
                session.exec(
                    select(StepEvidence).where(
                        StepEvidence.step_id == step_id,
                        StepEvidence.conclusion == "RETRY_REQUESTED",
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_late_receipt_between_request_and_claim_always_stops_retry(
    executable, after_dispatch
):
    from app.integrations.tiktok.contracts.builds import CreatedObject
    from app.modules.builds.execution_state import record_created

    db, context, _ = executable
    submission_id, step_id, old = failed_unsent(executable)
    receipt = request(executable, submission_id)
    if after_dispatch:
        run(executable, receipt)
    with Session(db) as session, session.begin():
        assert (
            record_created(
                session,
                claim=old,
                result=CreatedObject(
                    kind="CTA",
                    remote_id="late-existing-cta",
                    operation_status=None,
                    evidence=CallEvidence(request_id="late-receipt"),
                ),
            )
            == "UNKNOWN"
        )
    run(executable, receipt)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        assert step.status == "UNKNOWN" and not step.remote_id
        assert (
            claim_step(session, context=context, step_id=step.id, owner=uuid4()) is None
        )
        assert step.attempt_id == old.attempt_id
        assert (
            session.exec(
                select(StepEvidence).where(
                    StepEvidence.step_id == step_id,
                    StepEvidence.conclusion == "LATE_CREATED",
                )
            )
            .one()
            .summary["remote_id"]
            == "late-existing-cta"
        )


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_digest",
        "wrong_attempt",
        "missing_nonce",
        "duplicate_end",
        "foreign_audit",
        "old_attempt_effect",
    ],
)
def test_candidate_and_worker_proof_reject_corrupt_or_incomplete_history(
    executable, damage
):
    db, context, _ = executable
    submission_id, step_id, old = failed_unsent(executable)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        summary = {"body_digest": step.request_body_digest}
        claim = old
        conclusion = "NOT_SENT"
        if damage == "wrong_digest":
            summary["body_digest"] = "0" * 64
        elif damage == "wrong_attempt":
            claim = old.model_copy(update={"attempt_id": uuid4()})
            # 正常 evidence() 会拒绝错误 attempt，直接增加不可变损坏夹具。
        elif damage == "missing_nonce":
            claim = None
        elif damage == "foreign_audit":
            claim, conclusion = None, "RETRY_REQUESTED"
            summary.update(attempt_id=str(old.attempt_id), recovery_id=str(uuid4()))
        elif damage == "old_attempt_effect":
            claim = old.model_copy(update={"attempt": old.attempt - 1})
            save_attempt_context(session, step=step, attempt=claim.attempt)
            conclusion = "RESULT_UNKNOWN"
        session.add(
            StepEvidence(
                tenant_id=step.tenant_id,
                submission_id=submission_id,
                step_id=step_id,
                attempt=claim.attempt if claim else step.attempt,
                lease_token=claim.lease_token if claim else None,
                conclusion=conclusion,
                summary={
                    **summary,
                    **({"attempt_id": str(claim.attempt_id)} if claim else {}),
                },
            )
        )
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert not safely_unsent_attempt(session, step=step, recovery=True)
        assert not recovery.recovery_summary(
            session, context=context, submission=session.get(Submission, submission_id)
        ).can_retry


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("failed_kind", ["CTA", "CAMPAIGN", "ADGROUP", "AD"])
def test_mcp_exhaustion_manual_retry_then_all_original_children_create(
    database_engine, redis_client, channel_execution, monkeypatch, failed_kind
):
    from app.integrations.tiktok.mcp import transport
    from app.modules.builds.execution import process_step
    from tests.integrations.tiktok.gateway_support import business_calls

    case, wire = channel_execution
    original = transport._new_http_transport
    failures_left = 0

    class InterruptedCatalog(original):
        async def handle_async_request(self, request):
            nonlocal failures_left
            message = json.loads(request.content) if request.method == "POST" else {}
            if message.get("method") == "tools/list" and failures_left:
                failures_left -= 1
                raise httpx2.ReadTimeout("synthetic private diagnostic")
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", InterruptedCatalog)
    env = (database_engine, case["context"], {})

    def execute(kind):
        with Session(database_engine) as session:
            revision = session.get(ExecutionStep, case["ids"][kind]).dispatch_revision
        return process_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=case["context"],
            step_id=case["ids"][kind],
            revision=revision,
        )

    kinds = ("CTA", "CAMPAIGN", "ADGROUP", "AD")
    failed_index = kinds.index(failed_kind)
    failed_step_id = case["ids"][failed_kind]
    for kind in kinds[:failed_index]:
        created(wire, kind)
        assert execute(kind) == "SUCCEEDED"
    failures_left = 3
    for expected in ("PENDING", "PENDING", "FAILED"):
        assert execute(failed_kind) == expected
        with Session(database_engine) as session, session.begin():
            session.get(ExecutionStep, failed_step_id).due_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    assert len(business_calls(wire, "OFFICIAL_MCP")) == failed_index
    for _ in range(3):
        tick(env, case["unit_id"])
    with Session(database_engine) as session:
        step = session.get(ExecutionStep, failed_step_id)
        frozen = (
            step.request_body,
            step.request_body_digest,
            step.attempt,
            step.attempt_id,
        )
        assert step.resolved["transport_failure_count"] == 3
        assert all(
            session.get(ExecutionStep, case["ids"][kind]).status == "FAILED"
            for kind in kinds[failed_index:]
        )
    receipt = request(env, case["submission_id"])
    run(env, receipt)
    failures_left = 1
    assert execute(failed_kind) == "PENDING"  # 新显式恢复重新获得三次上限。
    assert request(env, case["submission_id"], request_id=receipt.request_id) == receipt
    run(env, receipt)  # 旧请求重放不能清掉新一轮已发生的失败计数。
    with Session(database_engine) as session, session.begin():
        step = session.get(ExecutionStep, failed_step_id)
        assert step.resolved["transport_failure_count"] == 1
        step.due_at = datetime.now(UTC) - timedelta(seconds=1)
    for kind in kinds[failed_index:]:
        if kind != failed_kind:
            tick(env, case["unit_id"])
            with Session(database_engine) as session:
                assert session.get(ExecutionStep, case["ids"][kind]).status == "QUEUED"
        created(wire, kind)
        assert execute(kind) == "SUCCEEDED"
        assert execute(kind) == "SUCCEEDED"
    with Session(database_engine) as session:
        step = session.get(ExecutionStep, failed_step_id)
        assert (
            step.request_body,
            step.request_body_digest,
            step.attempt,
            step.attempt_id,
        ) == frozen
        assert (
            session.get(ExecutionStep, case["ids"]["ADGROUP"]).request_body[
                "campaign_id"
            ]
            == "synthetic-campaign"
        )
        assert (
            session.get(ExecutionStep, case["ids"]["AD"]).request_body["adgroup_id"]
            == "synthetic-adgroup"
        )
        assert session.get(ExecutionStep, case["ids"]["AD"]).request_body[
            "creative_list"
        ][0]["creative_info"]["image_info"] == [{"web_uri": "synthetic-cover"}]
    assert len(business_calls(wire, "OFFICIAL_MCP")) == 4
