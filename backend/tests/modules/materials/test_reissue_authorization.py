"""显式素材补发授权不得变成普通 UNKNOWN 或广告的重试入口。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.materials.reissue import (
    MaterialReissueInput,
    authorize_material_reissue,
)
from tests.modules.builds.test_definite_material_recovery import (
    rejected_material as rejected_material,
)
from tests.modules.builds.test_execution import executable as executable
from tests.modules.materials.test_video_reissue import unknown_video as unknown_video
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def _approve(session, env, unknown, body, submission_id=None):
    return authorize_material_reissue(
        session,
        tenant_id=env[1].tenant_id,
        actor_id=env[1].actor_id,
        submission_id=submission_id or unknown[1],
        body=body,
    )


def _body(unknown, **changes):
    return MaterialReissueInput(
        **{
            "request_id": uuid4(),
            "kind": "VIDEO",
            "old_id": unknown[2],
            "accepted_duplicate_materials": True,
            **changes,
        }
    )


def test_same_request_returns_original_receipt_and_rejects_scope_change(
    executable, unknown_video
):
    body = _body(unknown_video)
    with Session(executable[0]) as session, session.begin():
        first = _approve(session, executable, unknown_video, body)
    with Session(executable[0]) as session, session.begin():
        assert _approve(session, executable, unknown_video, body) == first
        with pytest.raises(
            DomainError, check=lambda e: e.code == "request_id_conflict"
        ):
            _approve(session, executable, unknown_video, body, submission_id=uuid4())


def test_reissue_transaction_rollback_preserves_old_and_outbox(
    executable, unknown_video
):
    from app.jobs.models import PendingDispatch
    from app.modules.materials.models import MaterialDistribution
    from app.modules.materials.reissue_models import MaterialReissueAuthorization

    with Session(executable[0]) as session:
        before = set(session.exec(select(PendingDispatch.id)).all())
    with (
        pytest.raises(RuntimeError),
        Session(executable[0]) as session,
        session.begin(),
    ):
        _approve(session, executable, unknown_video, _body(unknown_video))
        raise RuntimeError("模拟提交前进程失败")
    with Session(executable[0]) as session:
        assert (
            session.get(MaterialDistribution, unknown_video[2]).superseded_by_id is None
        )
        assert session.exec(select(MaterialReissueAuthorization)).all() == []
        assert set(session.exec(select(PendingDispatch.id)).all()) == before


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_approvals_create_one_replacement(
    executable, unknown_video, same_request
):
    from app.modules.materials.models import MaterialDistribution
    from app.modules.materials.reissue_models import MaterialReissueAuthorization

    first = _body(unknown_video)
    bodies = [first, first if same_request else _body(unknown_video)]
    barrier = Barrier(2)

    def work(body):
        with Session(executable[0]) as session, session.begin():
            barrier.wait(timeout=10)
            try:
                return _approve(session, executable, unknown_video, body)
            except DomainError as error:
                assert error.code == "material_reissue_busy"
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, bodies))
    successes = [result for result in results if result]
    assert successes
    with Session(executable[0]) as session, session.begin():
        for body in bodies:
            assert _approve(session, executable, unknown_video, body) == successes[0]
        assert len(session.exec(select(MaterialReissueAuthorization)).all()) == 1
        assert len(session.exec(select(MaterialDistribution)).all()) == 2


@pytest.mark.parametrize("kind", ["AD", "CAMPAIGN", "ALL", ""])
def test_reissue_input_has_no_ad_or_wildcard_operation(kind):
    with pytest.raises(ValidationError):
        MaterialReissueInput(
            request_id=uuid4(),
            kind=kind,
            old_id=uuid4(),
            accepted_duplicate_materials=True,
        )


@pytest.mark.parametrize("accepted", [False, None, "true", 1])
def test_reissue_requires_explicit_boolean_acceptance(accepted):
    with pytest.raises(ValidationError):
        MaterialReissueInput(
            request_id=uuid4(),
            kind="VIDEO",
            old_id=uuid4(),
            accepted_duplicate_materials=accepted,
        )


def test_operator_cannot_authorize_duplicate_material_risk(executable):
    database, context, _ = executable
    with Session(database) as session, session.begin():
        with pytest.raises(DomainError, check=lambda e: e.code == "action_forbidden"):
            authorize_material_reissue(
                session,
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                submission_id=uuid4(),
                body=MaterialReissueInput(
                    request_id=uuid4(),
                    kind="VIDEO",
                    old_id=uuid4(),
                    accepted_duplicate_materials=True,
                ),
            )


def test_http_reissue_uses_authenticated_admin_and_returns_stable_receipt(
    executable, unknown_video
):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.security import create_access_token
    from app.main import app

    def database():
        with Session(executable[0]) as session:
            yield session

    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database
    body = _body(unknown_video).model_dump(mode="json")
    prefix = f"/api/tenants/{executable[1].tenant_id}/submissions/{unknown_video[1]}/material-reissues"
    token = create_access_token(str(executable[1].actor_id), timedelta(minutes=5))
    try:
        with TestClient(app) as client:
            assert client.post(prefix, json=body).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            assert client.post(prefix, json={**body, "kind": "AD"}).status_code == 422
            first = client.post(prefix, json=body)
            assert first.status_code == 202, first.json()
            assert first.json()["old_id"] == str(unknown_video[2])
            assert first.json()["replacement_id"] != str(unknown_video[2])
            assert client.post(prefix, json=body).json() == first.json()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
