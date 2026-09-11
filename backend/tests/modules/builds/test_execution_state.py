"""Real PostgreSQL attempt boundaries and late-result evidence; no SDK calls."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.execution_models import (
    ExecutionStep,
    StepEvidence,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.previews import generate_preview, get_preview_units
from app.modules.builds.routes import load_preview_route, save_attempt_context
from app.modules.builds.sdk_requests import RemoteCreated
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


@pytest.fixture
def attempt(session, context, prepared):
    preview = generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    unit = get_preview_units(session, context=context, preview_id=preview).items[0]
    sub = Submission(
        tenant_id=context.tenant_id,
        bc_id="bc-draft",
        preview_id=preview,
        draft_id=prepared,
        actor_id=context.actor_id,
        ordinal=1,
    )
    session.add(sub)
    session.flush()
    session.add(
        SubmissionUnit(
            tenant_id=context.tenant_id,
            submission_id=sub.id,
            unit_id=unit.unit_id,
            preview_id=preview,
            bc_id="bc-draft",
            disposition="INCLUDED",
            expanded=True,
        )
    )
    session.flush()
    nonce = uuid4()
    expires = datetime.now(UTC) + timedelta(seconds=60)
    step = ExecutionStep(
        tenant_id=context.tenant_id,
        submission_id=sub.id,
        preview_id=preview,
        bc_id="bc-draft",
        unit_id=unit.unit_id,
        kind="CAMPAIGN",
        step_key="campaign",
        status="RUNNING",
        phase="CLAIMED",
        attempt=1,
        lease_token=nonce,
        lease_expires_at=expires,
    )
    session.add(step)
    attempt_id = save_attempt_context(session, step=step)
    session.flush()
    claim = StepClaim(
        step_id=step.id,
        tenant_id=context.tenant_id,
        submission_id=sub.id,
        preview_id=preview,
        unit_id=unit.unit_id,
        actor_id=context.actor_id,
        bc_id="bc-draft",
        advertiser_id=unit.advertiser_id,
        kind="CAMPAIGN",
        group_id=None,
        planned_ad_id=None,
        material_id=None,
        parent_step_id=None,
        lease_token=nonce,
        lease_expires_at=expires,
        attempt=1,
        attempt_id=attempt_id,
        route=load_preview_route(session, context=context, preview_id=preview),
        dispatch_revision=0,
    )
    return step, claim


def body(claim):
    return {
        "advertiser_id": claim.advertiser_id,
        "campaign_name": "frozen",
        "operation_status": "ENABLE",
        "budget": 100,
    }


def test_arm_persists_full_body_and_receipt_saves_id_before_any_child(
    session, context, attempt
):
    from app.modules.builds.execution_state import arm_request, record_created

    step, claim = attempt
    digest = arm_request(session, context=context, claim=claim, body=body(claim))
    session.flush()
    assert step.phase == "REQUEST_ARMED" and step.request_body == body(claim)
    assert digest == step.request_body_digest and len(digest) == 64
    assert (
        record_created(
            session,
            claim=claim,
            result=RemoteCreated("remote-c1", "request1", "ENABLE"),
        )
        == "SUCCEEDED"
    )
    session.flush()
    assert (
        step.remote_id == "remote-c1"
        and step.status == "SUCCEEDED"
        and step.phase == "DONE"
    )
    events = session.exec(
        select(StepEvidence).where(StepEvidence.step_id == step.id)
    ).all()
    assert {e.conclusion for e in events} == {"REQUEST_ARMED", "CREATED"}
    assert all("Access-Token" not in e.summary for e in events)


def test_expired_armed_attempt_can_only_become_unknown(session, context, attempt):
    from app.modules.builds.execution_state import arm_request, expire_attempt

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert expire_attempt(session, step=step) == "RECONCILE"
    session.flush()
    assert step.status == "UNKNOWN" and step.request_body == body(claim)


def test_expired_unarmed_attempt_is_safely_reclaimable(session, attempt):
    from app.modules.builds.execution_state import expire_attempt

    step, _ = attempt
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert expire_attempt(session, step=step) == "RECLAIM"
    session.flush()
    assert (
        step.status == "PENDING" and step.phase == "IDLE" and step.lease_token is None
    )


def test_stale_worker_cannot_arm_or_replace_a_new_owner(session, context, attempt):
    from app.modules.builds.execution_state import arm_request, record_created

    step, old = attempt
    step.lease_token = uuid4()
    step.attempt = 2
    save_attempt_context(session, step=step)
    session.flush()
    with pytest.raises(DomainError) as caught:
        arm_request(session, context=context, claim=old, body=body(old))
    assert caught.value.code == "execution_lease_lost"
    current_nonce = step.lease_token
    assert (
        record_created(
            session,
            claim=old,
            result=RemoteCreated("late-id", "late-request", "ENABLE"),
        )
        == "UNKNOWN"
    )
    session.flush()
    assert step.remote_id is None and step.lease_token == current_nonce
    evidence = session.exec(
        select(StepEvidence).where(
            StepEvidence.step_id == step.id, StepEvidence.conclusion == "LATE_CREATED"
        )
    ).one()
    assert evidence.summary["remote_id"] == "late-id" and evidence.attempt == 1


def test_arm_rejects_cross_tenant_and_non_json_intent(
    session, context, other_context, attempt
):
    from app.modules.builds.execution_state import arm_request

    step, claim = attempt
    with pytest.raises(DomainError):
        arm_request(session, context=other_context, claim=claim, body=body(claim))
    with pytest.raises(DomainError):
        arm_request(
            session,
            context=context,
            claim=claim,
            body={**body(claim), "budget": float("nan")},
        )
    assert step.phase == "CLAIMED" and step.request_body is None


def test_unknown_result_keeps_original_body_and_success_never_regresses(
    session, context, attempt
):
    from app.modules.builds.execution_state import (
        arm_request,
        record_created,
        record_unknown,
    )

    step, claim = attempt
    arm_request(session, context=context, claim=claim, body=body(claim))
    assert (
        record_unknown(
            session,
            claim=claim,
            code="create_result_unknown",
            request_id="lost",
            remote_code=-1,
        )
        == "UNKNOWN"
    )
    session.flush()
    assert step.request_body == body(claim) and step.remote_id is None
    record_created(
        session, claim=claim, result=RemoteCreated("eventual-id", "receipt", "ENABLE")
    )
    session.flush()
    # A late known ID is evidence for reconciliation, never a blind state overwrite.
    assert step.status == "UNKNOWN" and step.remote_id is None
