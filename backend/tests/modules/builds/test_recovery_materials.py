"""Manual MATERIAL recovery stays in the original distribution read path."""

import pytest
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.builds import recovery
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import load_preview_route
from app.modules.materials.distribution import run_distribution
from app.modules.materials.models import (
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialUploadAttempt,
)
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery import request, run


def unknown_material(env, *, source=False):
    db, context, ids = env
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        route = load_preview_route(session, context=context, preview_id=step.preview_id)
        unit = session.get(BuildUnit, step.unit_id)
        op = MaterialAssetOperation(
            tenant_id=context.tenant_id,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id=unit.advertiser_id,
            path="upload_original",
            status="result_unknown",
            frozen_route=route.model_dump(mode="json"),
            request_digest="a" * 64,
        )
        session.add(op)
        session.flush()
        dist = MaterialDistribution(
            tenant_id=context.tenant_id,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id=unit.advertiser_id,
            actor_id=context.actor_id,
            path="upload_original",
            status="result_unknown",
            operation_id=op.id,
            target_route=route.model_dump(mode="json"),
        )
        session.add(dist)
        session.flush()
        step.status, step.phase, step.distribution_id = "UNKNOWN", "DONE", dist.id
        if source:
            session.add(
                MaterialUploadAttempt(
                    tenant_id=context.tenant_id,
                    bc_id=step.bc_id,
                    material_id=step.material_id,
                    advertiser_id=unit.advertiser_id,
                    connection_id=unit.connection_id,
                    operation_id=op.id,
                    request_digest="a" * 64,
                    status="result_unknown",
                )
            )
        session.add(step)
        return step.submission_id, step.id, dist.id, op.id


def test_material_unknown_queues_only_strict_original_verify(executable):
    db, context, _ = executable
    submission_id, step_id, dist_id, op_id = unknown_material(executable)
    receipt = request(executable, submission_id, kind="RECONCILE")
    run(executable, receipt)
    with Session(db) as session:
        current = recovery.get_recovery(
            session, context=context, recovery_id=receipt.recovery_id
        )
        assert current.state == "COMPLETED" and current.scheduled_count == 1
        step = session.get(ExecutionStep, step_id)
        assert step.status == "UNKNOWN" and step.dispatch_id is None
        messages = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "materials.verify_target"
            )
        ).all()
        assert len(messages) == 1
        assert messages[0].payload == {
            "distribution_id": str(dist_id),
            "operation_id": str(op_id),
            "observe": True,
            "read_only": True,
            "revision": 0,
        }
        assert messages[0].actor_id == context.actor_id
        assert (
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "builds.reconcile_step"
                )
            ).all()
            == []
        )


@pytest.mark.parametrize("source", [False, True])
def test_delayed_manual_verify_never_falls_back_after_operation_failed(
    executable, redis_client, source
):
    db, context, _ = executable
    submission_id, _, dist_id, op_id = unknown_material(executable, source=source)
    receipt = request(executable, submission_id, kind="RECONCILE")
    run(executable, receipt)
    with Session(db) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.status, op.remote_response = "failed", {"definite_no_effect": True}
        session.add(op)
        if source:
            attempt = session.exec(
                select(MaterialUploadAttempt).where(
                    MaterialUploadAttempt.operation_id == op_id
                )
            ).one()
            attempt.status = "failed"
            session.add(attempt)
        # Remove existing fixture mappings so a delayed source observer cannot
        # exit as locally ready before exercising the failed-operation branch.
        from app.modules.materials.models import AccountMaterial

        for asset in session.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == op.material_id)
        ).all():
            asset.status = "unavailable"
            session.add(asset)
    run_distribution(
        database_engine=db,
        redis_client=redis_client,
        context=context,
        distribution_id=dist_id,
        operation_id=op_id,
        kind="verify",
        read_only=True,
    )
    with Session(db) as session:
        assert session.get(MaterialDistribution, dist_id).status == "blocked"
        assert session.get(MaterialDistribution, dist_id).operation_id == op_id
        assert (
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "materials.prepare_target"
                )
            ).all()
            == []
        )
        assert len(session.exec(select(MaterialAssetOperation)).all()) == 1


def test_failed_material_with_unknown_operation_cannot_retry_upload(executable):
    db, _, _ = executable
    submission_id, step_id, dist_id, _ = unknown_material(executable)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, step_id)
        step.status, step.error_code = "FAILED", "action_forbidden"
        dist = session.get(MaterialDistribution, dist_id)
        dist.status = "blocked"
        session.add_all([step, dist])
    from app.core.errors import DomainError

    with pytest.raises(DomainError, check=lambda e: e.code == "recovery_no_candidates"):
        request(executable, submission_id)

    receipt = request(executable, submission_id, kind="RECONCILE")
    run(executable, receipt)
    with Session(db) as session:
        assert session.get(ExecutionStep, step_id).dispatch_id is None
        assert (
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "materials.prepare_target"
                )
            ).all()
            == []
        )
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.task_name == "materials.verify_target"
                    )
                ).all()
            )
            == 1
        )
