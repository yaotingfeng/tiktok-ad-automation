from dataclasses import replace
from uuid import uuid4

from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.builds import execution
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.scene_job_models import SceneJob
from app.modules.builds.scene_schemas import SceneContext
from app.modules.providers.models import ProviderApplication, ProviderConnection
from tests.modules.builds.test_execution import executable as executable


def test_expired_scene_commits_preparation_without_arming_any_ad(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    with Session(db) as session, session.begin():
        provider = session.exec(select(ProviderConnection)).one()
        provider.verification_token = uuid4()
        app = session.exec(select(ProviderApplication)).one()
        app.channel_config = {"verification_token": str(provider.verification_token)}
        app.tiktok_minis_id = "minis-1"
        session.add_all([provider, app])
    current = execution.read_scene_context(None)
    monkeypatch.setattr(
        execution,
        "read_scene_context",
        lambda *_a, **_k: replace(
            current, supported=False, reason_codes=("scene_evidence_expired",)
        ),
    )

    def no_sdk(*_args, **_kwargs):
        raise AssertionError("scene dependency must only enqueue local work")

    monkeypatch.setattr("business_api_client.ApiClient.call_api", no_sdk)
    assert (
        execution.process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=ids["CTA"][0],
            revision=0,
        )
        == "PENDING"
    )
    with Session(db) as session:
        job = session.exec(select(SceneJob)).one()
        assert job.status == "PENDING"
        assert (
            session.get(PendingDispatch, job.dispatch_id).task_name
            == "builds.refresh_scene"
        )
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.status == "PENDING" and step.request_body is None
        assert step.error_code == "scene_refresh_required"


def test_unrecoverable_scene_dependency_is_explicit_local_failure(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    monkeypatch.setattr(
        execution,
        "read_scene_context",
        lambda *_a, **_k: SceneContext(
            supported=False,
            reason_codes=("scene_evidence_missing",),
            capability_revision="offline-v1",
        ),
    )
    assert (
        execution.process_step(
            database_engine=db,
            redis_client=redis_client,
            context=context,
            step_id=ids["CTA"][0],
            revision=0,
        )
        == "FAILED"
    )
    with Session(db) as session:
        step = session.get(ExecutionStep, ids["CTA"][0])
        assert step.request_body is None
        assert step.error_code == "scene_link_unavailable"
        assert not session.exec(select(SceneJob)).all()
