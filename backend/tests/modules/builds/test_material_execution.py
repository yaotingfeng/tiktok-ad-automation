from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.material_execution import recover_material_results
from app.modules.builds.routes import load_preview_route
from app.modules.materials.models import AccountMaterial, MaterialDistribution
from tests.modules.builds.test_execution import executable as executable
from tests.modules.builds.test_recovery_materials import unknown_material


def unresolved(db, context, identity, *, status="result_unknown"):
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        route = load_preview_route(session, context=context, preview_id=step.preview_id)
        dist = MaterialDistribution(
            tenant_id=context.tenant_id,
            bc_id=step.bc_id,
            material_id=step.material_id,
            advertiser_id="account-A",
            actor_id=context.actor_id,
            path="original_upload",
            target_route=route.model_dump(mode="json"),
            status=status,
        )
        session.add(dist)
        session.flush()
        step.status, step.phase, step.distribution_id = "UNKNOWN", "DONE", dist.id
        session.add(step)
        return dist.id


def test_unknown_material_resumes_only_after_verified_target_result(
    executable, monkeypatch
):
    db, context, ids = executable
    dist_id = unresolved(db, context, ids["MATERIAL"][0])
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session, session.begin():
        dist = session.get(MaterialDistribution, dist_id)
        dist.status = "ready"
        session.add(dist)

    def no_sdk(*_args, **_kwargs):
        raise AssertionError("a verified mapping must not create/upload again")

    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_sdk)
    assert recover_material_results(database_engine=db) == 1
    assert recover_material_results(database_engine=db) == 0
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


def test_historic_ready_distribution_cannot_trigger_upload_when_mapping_is_stale(
    executable, monkeypatch
):
    db, context, ids = executable
    unresolved(db, context, ids["MATERIAL"][0], status="ready")
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        asset = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == step.material_id
            )
        ).one()
        asset.verified_at = datetime.now(UTC) - timedelta(hours=1)
        session.add(asset)

    def no_effect(*_args, **_kwargs):
        raise AssertionError("unknown upload cannot schedule or send another upload")

    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_effect)
    monkeypatch.setattr(
        "app.modules.materials.distribution.ensure_target_asset", no_effect
    )
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["MATERIAL"][0])
        assert step.status == "UNKNOWN" and step.dispatch_id is None


def test_blocked_distribution_does_not_erase_ambiguous_original_upload(executable):
    db, _, _ = executable
    _, step_id, dist_id, _ = unknown_material(executable)
    with Session(db) as session, session.begin():
        dist = session.get(MaterialDistribution, dist_id)
        dist.status = "blocked"
        session.add(dist)
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session:
        step = session.get(ExecutionStep, step_id)
        assert step.status == "UNKNOWN" and step.dispatch_id is None
