from sqlmodel import Session

from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.material_execution import recover_material_results
from app.modules.builds.tasks import deliver_step
from app.modules.materials.models import MaterialDistribution
from tests.modules.builds.test_execution import executable as executable


def unresolved(db, context, identity, *, status="result_unknown"):
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        dist = MaterialDistribution(
            tenant_id=context.tenant_id,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id="account-A",
            actor_id=context.actor_id,
            path="original_upload",
            status=status,
        )
        session.add(dist)
        session.flush()
        step.status, step.phase, step.distribution_id = "UNKNOWN", "DONE", dist.id
        session.add(step)
        return dist.id


def test_unknown_material_resumes_only_after_verified_target_result(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    dist_id = unresolved(db, context, ids["MATERIAL"][0])
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session, session.begin():
        dist = session.get(MaterialDistribution, dist_id)
        dist.status = "ready"
        session.add(dist)
    assert recover_material_results(database_engine=db) == 1
    assert recover_material_results(database_engine=db) == 0

    def no_sdk(*_args, **_kwargs):
        raise AssertionError("a verified mapping must not create/upload again")

    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_sdk)
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        assert step.status == "QUEUED"
        payload = {"step_id": str(step.id), "revision": step.dispatch_revision}
    deliver_step(
        database_engine=db, redis_client=redis_client, context=context, payload=payload
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        assert step.status == "SUCCEEDED"
        assert step.resolved["mapping"]["video_id"] in {"target-0", "target-1"}


def test_blocked_material_is_local_failure_and_unrelated_unknown_stays_unknown(
    executable,
):
    db, context, ids = executable
    unresolved(db, context, ids["MATERIAL"][0], status="blocked")
    unresolved(db, context, ids["MATERIAL"][1])
    assert recover_material_results(database_engine=db) == 1
    with Session(db) as session:
        first = session.get(ExecutionStep, ids["MATERIAL"][0])
        second = session.get(ExecutionStep, ids["MATERIAL"][1])
        assert first.status == "FAILED" and first.request_body is None
        assert second.status == "UNKNOWN" and second.dispatch_id is None
