from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.builds.execution_models import ExecutionStep, SubmissionUnit
from tests.modules.builds.test_execution import executable as executable


def test_unit_dispatches_only_ready_steps_and_duplicate_delivery_is_noop(executable):
    from app.modules.builds.dispatch import process_unit

    db, context, ids = executable
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
    assert process_unit(database_engine=db, context=context, payload=payload) == 3
    assert process_unit(database_engine=db, context=context, payload=payload) == 0
    with Session(db) as session:
        queued = session.exec(
            select(ExecutionStep).where(ExecutionStep.status == "QUEUED")
        ).all()
        assert sorted(s.kind for s in queued) == ["CTA", "MATERIAL", "MATERIAL"]
        assert all(s.request_body is None and s.phase == "IDLE" for s in queued)
        for step in queued:
            message = session.get(PendingDispatch, step.dispatch_id)
            assert (
                message.actor_id == context.actor_id
                and message.tenant_id == context.tenant_id
            )
            assert message.payload == {
                "step_id": str(step.id),
                "revision": step.dispatch_revision,
            }


def test_repair_preserves_current_identity_and_broker_backoff(executable):
    from app.modules.builds.dispatch import process_unit, repair_execution

    db, context, _ = executable
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
    process_unit(database_engine=db, context=context, payload=payload)
    with Session(db) as session, session.begin():
        step = session.exec(
            select(ExecutionStep).where(ExecutionStep.kind == "CTA")
        ).one()
        step.due_at = datetime.now(UTC) - timedelta(seconds=200)
        step.updated_at = step.due_at
        msg = session.get(PendingDispatch, step.dispatch_id)
        original = (step.id, step.dispatch_id, step.dispatch_revision)
        msg.available_at = datetime.now(UTC) + timedelta(seconds=100)
        future = msg.available_at
        session.add_all([step, msg])
    assert repair_execution(database_engine=db) >= 1
    with Session(db) as session:
        step = session.get(ExecutionStep, original[0])
        assert (step.dispatch_id, step.dispatch_revision) == original[1:]
        assert session.get(PendingDispatch, step.dispatch_id).available_at == future
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, original[0])
        step.updated_at = datetime.now(UTC) - timedelta(seconds=200)
        msg = session.get(PendingDispatch, step.dispatch_id)
        msg.published_at = datetime.now(UTC) - timedelta(seconds=180)
        session.add_all([step, msg])
    assert repair_execution(database_engine=db) >= 1
    with Session(db) as session:
        step = session.get(ExecutionStep, original[0])
        msg = session.get(PendingDispatch, step.dispatch_id)
        assert (step.dispatch_id, step.dispatch_revision) == original[1:]
        assert msg.published_at is None
        assert step.due_at <= datetime.now(UTC)
        assert msg.available_at <= datetime.now(UTC)


def test_failed_cta_propagates_through_all_layers_without_sdk(executable):
    from app.modules.builds.dispatch import process_unit
    from app.modules.builds.execution_models import Submission

    db, context, ids = executable
    with Session(db) as session, session.begin():
        for step_id in [*ids["MATERIAL"], *ids["CTA"]]:
            step = session.get(ExecutionStep, step_id)
            step.status, step.phase = "FAILED", "DONE"
            session.add(step)
    for _ in range(12):
        with Session(db) as session:
            unit = session.exec(select(SubmissionUnit)).one()
            if unit.dispatch_id is None:
                break
            payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
        assert process_unit(database_engine=db, context=context, payload=payload) == 0
    else:
        raise AssertionError("bounded dependency propagation did not settle")
    with Session(db) as session:
        steps = session.exec(select(ExecutionStep)).all()
        assert all(s.status == "FAILED" for s in steps)
        assert all(s.request_body is None for s in steps)
        assert session.exec(select(Submission)).one().status == "FAILED"


def test_revoked_actor_settles_unarmed_steps_without_remote_work(
    executable, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.builds import dispatch

    db, context, _ = executable

    def denied(*_args, **_kwargs):
        raise DomainError("action_forbidden", "denied")

    monkeypatch.setattr(dispatch, "require_tenant", denied)
    with Session(db) as session:
        unit = session.exec(select(SubmissionUnit)).one()
        payload = {"unit_id": str(unit.unit_id), "revision": unit.dispatch_revision}
    assert (
        dispatch.process_unit(database_engine=db, context=context, payload=payload) == 0
    )
    with Session(db) as session:
        assert all(
            s.status == "FAILED" and s.request_body is None
            for s in session.exec(select(ExecutionStep)).all()
        )


def test_expired_armed_write_is_queued_only_for_reconciliation(executable):
    from app.modules.builds.dispatch import repair_execution
    from app.modules.builds.execution_state import arm_request
    from app.modules.builds.submissions import claim_step

    db, context, ids = executable
    with Session(db) as session, session.begin():
        claim = claim_step(
            session, context=context, step_id=ids["CTA"][0], owner=uuid4()
        )
        arm_request(
            session,
            context=context,
            claim=claim,
            body={
                "advertiser_id": "account-A",
                "creative_portfolio_type": "CTA",
                "portfolio_content": [
                    {"asset_ids": ["cta-1"], "asset_content": "Watch now"}
                ],
            },
        )
        step = session.get(ExecutionStep, claim.step_id)
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        step.due_at = datetime.now(UTC) - timedelta(seconds=200)
        session.add(step)
    repair_execution(database_engine=db)
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "UNKNOWN" and step.request_body
        msg = session.get(PendingDispatch, step.dispatch_id)
        assert msg.task_name == "builds.reconcile_step"
