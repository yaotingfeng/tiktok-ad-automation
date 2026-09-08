import importlib.util

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_db
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.models import User
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.tenants.models import TenantMembership


def test_public_api_commits_original_job_and_get_is_viewer_read_only(
    capability_env, wire
):
    assert importlib.util.find_spec("app.modules.accounts.capability_router"), (
        "Capability API missing"
    )
    from app.modules.accounts.capability_router import router

    env = capability_env
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def session_dependency():
        with Session(engine) as session:
            yield session

    def user_dependency():
        with Session(engine) as session:
            return session.get(User, env["context"].actor_id)

    app.dependency_overrides[get_db] = session_dependency
    app.dependency_overrides[get_current_user] = user_dependency
    base = (
        f"/api/tenants/{env['context'].tenant_id}/bcs/{env['bc_id']}/capability-refresh"
    )
    with TestClient(app) as client:
        body = {
            "request_id": str(env["request_id"]),
            "connection_id": str(env["connection_id"]),
        }
        first = client.post(base, json=body)
        assert first.status_code == 200
        data = first.json()
        assert data["job_id"]
        assert client.post(base, json=body).json()["job_id"] == data["job_id"]
        with Session(engine) as session, session.begin():
            assert session.get(CapabilityJob, data["job_id"]) is not None
            session.exec(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == env["context"].tenant_id
                )
            ).one().role = "viewer"
            count = len(session.exec(select(PendingDispatch)).all())
        response = client.get(base + "/" + data["job_id"])
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert (
            "scope" not in response.text
            and "ciphertext" not in response.text
            and "claim_token" not in response.text
        )
        assert response.json()["status"] == "PENDING"
        with Session(engine) as session:
            assert len(session.exec(select(PendingDispatch)).all()) == count
    assert not wire[0]
