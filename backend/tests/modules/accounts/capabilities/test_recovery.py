from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts import capabilities
from app.modules.accounts.capability_models import CapabilityJob
from tests.modules.accounts.capabilities.test_service import page, run, start


@pytest.mark.parametrize("code", ["mcp_contract_changed", "mcp_tool_unavailable"])
def test_contract_failure_blocks_capability_without_retry(
    capability_env, redis_client, monkeypatch, code
):
    from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError

    def reject_gateway(**_kwargs):
        raise RemoteCallError(code, effect="NOT_SENT", evidence=CallEvidence())

    monkeypatch.setattr(capabilities, "open_tiktok_gateway", reject_gateway)
    job_id = start(capability_env)
    job = run(capability_env, redis_client, job_id)
    assert job.status == "BLOCKED"
    assert job.error_code == code
    assert job.failure_count == 0
    assert job.claim_token is None and job.claimed_until is None


def test_repair_preserves_unpublished_backoff_and_original_delivery_identity(
    capability_env, wire, redis_client
):
    repair = getattr(capabilities, "repair_capabilities", None)
    assert repair is not None, "Durable capability recovery missing"
    env = capability_env
    job_id = start(env)
    with Session(engine) as session, session.begin():
        job = session.get(CapabilityJob, job_id)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.available_at = datetime.now(UTC) + timedelta(minutes=10)
        original = (
            dispatch.id,
            dict(dispatch.payload),
            job.revision,
            dispatch.available_at,
        )
    assert repair(database_engine=engine) == 1
    with Session(engine) as session, session.begin():
        job = session.get(CapabilityJob, job_id)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        assert (
            dispatch.id,
            dict(dispatch.payload),
            job.revision,
            dispatch.available_at,
        ) == original
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        job.claim_token = uuid4()
        job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        dispatch.published_at = datetime.now(UTC) - timedelta(minutes=5)
    assert repair(database_engine=engine) == 1
    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        assert (dispatch.id, dispatch.payload, job.revision) == original[:3]
        assert dispatch.published_at is None and job.claim_token is None
    wire[1].append(page(["actual-account"]))
    run(env, redis_client, job_id)
    assert run(env, redis_client, job_id).status == "COMPLETE"


def test_claim_and_receipt_are_fenced_across_all_authority_changes(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)

    def response():
        from app.modules.accounts.models import TikTokConnection

        with Session(engine) as session, session.begin():
            session.get(
                TikTokConnection, env["connection_id"]
            ).authorization_revision += 1
        return page(["actual-account"])

    wire[1].append(response)
    job = run(env, redis_client, job_id)
    assert job.status == "STALE"
