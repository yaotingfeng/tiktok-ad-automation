from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.capability_models import CapabilityAsset, CapabilityJob
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.tenants.models import TenantMembership
from tests.modules.accounts.capabilities.test_service import page, run, start


def test_network_holds_no_connection_or_job_lock_and_duplicate_has_no_get(
    capability_env, wire, redis_client
):
    env = capability_env
    job_id = start(env)
    entered, release = Event(), Event()

    def response():
        with Session(engine) as session, session.begin():
            assert (
                session.exec(
                    select(CapabilityJob)
                    .where(CapabilityJob.id == job_id)
                    .with_for_update(nowait=True)
                )
                .one()
                .claim_token
            )
            session.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == env["connection_id"])
                .with_for_update(nowait=True)
            ).one()
        entered.set()
        assert release.wait(5)
        return page(["actual-account"])

    wire[1].append(response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(run, env, redis_client, job_id)
        try:
            assert entered.wait(5)
            run(env, redis_client, job_id)
            assert len(wire[0]) == 1
        finally:
            release.set()
        assert pending.result(timeout=5).phase == "PUBLISH"


@pytest.mark.parametrize(
    "change", ["membership", "directory", "nonce", "deadline", "actor"]
)
def test_receipt_rejects_authority_or_claim_change(
    capability_env, wire, redis_client, change
):
    env = capability_env
    job_id = start(env)

    def response():
        with Session(engine) as session, session.begin():
            job = session.get(CapabilityJob, job_id)
            if change == "membership":
                session.exec(
                    select(TenantMembership).where(
                        TenantMembership.tenant_id == env["context"].tenant_id
                    )
                ).one().role = "viewer"
            elif change == "directory":
                session.exec(select(BCAccountAccess)).one().authorized = False
            elif change == "nonce":
                job.claim_token = uuid4()
            elif change == "deadline":
                job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
            else:
                from app.models import User

                other = User(username=f"{uuid4()}", hashed_password="unused")
                session.add(other)
                session.flush()
                job.actor_id = other.id
        return page(["actual-account"])

    wire[1].append(response)
    result = run(env, redis_client, job_id)
    assert result.phase == "READ"
    with Session(engine) as session:
        assert not session.exec(select(CapabilityAsset)).all()
        assert not session.exec(select(BCAccountAccess)).one().can_build
        if change == "actor":
            from app.models import User

            # restore FK so the standard fixture also removes the extra user
            actor = session.get(User, result.actor_id)
            job = session.get(CapabilityJob, job_id)
            job.actor_id = env["context"].actor_id
            session.flush()
            session.delete(actor)
            session.commit()
