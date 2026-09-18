from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.dependency_waits import wake_material_dependencies
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


def waiting(db, context, identity, *, status):
    dist_id = unresolved(db, context, identity, status=status)
    with Session(db) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        step.status, step.phase, step.error_code = "PENDING", "IDLE", "material_pending"
        step.dispatch_id = None
        session.add(step)
    return dist_id


def test_pending_material_terminal_result_settles_without_step_delivery(executable):
    db, context, ids = executable
    ready, blocked = ids["MATERIAL"][:2]
    waiting(db, context, ready, status="ready")
    waiting(db, context, blocked, status="blocked")

    # 通用唤醒不再把明确终态绕回 Builds Worker；恢复器在同一轮直接落定。
    assert wake_material_dependencies(database_engine=db) == 0
    assert recover_material_results(database_engine=db) == 2
    with Session(db) as session:
        ready_step = session.get(ExecutionStep, ready)
        blocked_step = session.get(ExecutionStep, blocked)
        assert (ready_step.status, ready_step.dispatch_id) == ("SUCCEEDED", None)
        assert (blocked_step.status, blocked_step.dispatch_id) == ("FAILED", None)


def test_pending_material_permission_denial_keeps_recovery_identity(
    executable, monkeypatch
):
    db, context, ids = executable
    identity = ids["MATERIAL"][0]
    waiting(db, context, identity, status="ready")

    def denied(*_args, **_kwargs):
        raise DomainError("account_access_denied", "denied")

    monkeypatch.setattr("app.modules.builds.material_execution.require_tenant", denied)
    assert recover_material_results(database_engine=db) == 0
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert (step.status, step.error_code, step.dispatch_id) == (
            "PENDING",
            "material_pending",
            None,
        )
        assert step.resolved["dependency_recovery_error"] == "account_access_denied"

    monkeypatch.undo()
    assert recover_material_results(database_engine=db) == 1
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert step.status == "SUCCEEDED"
        assert "dependency_recovery_error" not in step.resolved


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


def test_historic_ready_distribution_cannot_trigger_upload_when_mapping_is_unknown(
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
        asset.status = "result_unknown"
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


def test_exhausted_reconciliation_updates_unknown_reason_without_restarting(executable):
    db, context, ids = executable
    identity = ids["MATERIAL"][0]
    dist_id = unresolved(db, context, identity)
    with Session(db) as session, session.begin():
        dist = session.get(MaterialDistribution, dist_id)
        dist.reason_code = "material_reconciliation_budget_exhausted"
        session.add(dist)
        original = session.get(ExecutionStep, identity).request_body
    assert recover_material_results(database_engine=db) == 1
    assert recover_material_results(database_engine=db) == 0
    with Session(db) as session:
        step = session.get(ExecutionStep, identity)
        assert (step.status, step.phase) == ("UNKNOWN", "DONE")
        assert step.error_code == "material_reconciliation_budget_exhausted"
        assert step.dispatch_id is None
        assert step.request_body == original


def test_denied_budget_result_does_not_starve_next_recovery_page(
    executable, monkeypatch
):
    db, context, ids = executable
    for identity in ids["MATERIAL"][:2]:
        dist_id = unresolved(db, context, identity)
        with Session(db) as session, session.begin():
            dist = session.get(MaterialDistribution, dist_id)
            dist.reason_code = "material_reconciliation_budget_exhausted"
            session.add(dist)

            step = session.get(ExecutionStep, identity)
            step.error_code = "account_access_denied"
            session.add(step)

    def denied(*_args, **_kwargs):
        raise DomainError("account_access_denied", "denied")

    monkeypatch.setattr("app.modules.builds.material_execution.require_tenant", denied)
    assert recover_material_results(database_engine=db, limit=1) == 1
    assert recover_material_results(database_engine=db, limit=1) == 1
    assert recover_material_results(database_engine=db, limit=1) == 0
    with Session(db) as session:
        for identity in ids["MATERIAL"][:2]:
            step = session.get(ExecutionStep, identity)
            assert step.status == "UNKNOWN"
            assert step.error_code == "account_access_denied"
