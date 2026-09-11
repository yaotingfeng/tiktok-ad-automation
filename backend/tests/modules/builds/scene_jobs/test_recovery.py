from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from tests.modules.accounts.capabilities.test_service import page as role_page
from tests.modules.accounts.capabilities.test_service import run as run_capability
from tests.modules.builds.scene.test_scene_reads import scene_env as scene_env
from tests.modules.builds.scene_jobs.test_service import (
    complete,
    ensure,
    responses,
    run,
)
from tests.modules.builds.scene_jobs.test_service import (
    job_env as job_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def ready_for_get(env, wire, redis_client):
    result = ensure(env)
    job = run(env, redis_client, result.job_id)
    wire[1].append(role_page(["actual-account"]))
    run_capability(env, redis_client, job.capability_job_id)
    run_capability(env, redis_client, job.capability_job_id)
    return job


def test_two_deliveries_only_one_get_and_no_database_lock_across_transport(
    job_env, wire, redis_client
):
    env = job_env
    job = ready_for_get(env, wire, redis_client)
    entered, release = Event(), Event()

    def remote():
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout='200ms'"))
            session.exec(
                select(SceneJob).where(SceneJob.id == job.id).with_for_update()
            ).one()
            session.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == env["connection_id"])
                .with_for_update()
            ).one()
        entered.set()
        assert release.wait(3)
        return responses(env)[0]

    wire[1].append(remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, env, redis_client, job.id)
        assert entered.wait(3)
        second = pool.submit(run, env, redis_client, job.id)
        try:
            assert second.result(timeout=2).claim_token is not None
        finally:
            release.set()
        result = first.result(timeout=3)
    assert result.resource == "minis"
    assert len(wire[0]) == 2


@pytest.mark.parametrize(
    "change",
    ["nonce", "revision", "authorization", "role", "provider", "application", "grant"],
)
def test_stale_transport_receipt_cannot_publish(job_env, wire, redis_client, change):
    from app.modules.tenants.models import TenantMembership

    env = job_env
    job = ready_for_get(env, wire, redis_client)

    def remote():
        with Session(engine) as session, session.begin():
            current = session.get(SceneJob, job.id)
            if change == "nonce":
                current.claim_token = uuid4()
            elif change == "revision":
                current.revision += 1
            elif change == "authorization":
                session.get(
                    TikTokConnection, env["connection_id"]
                ).authorization_revision += 1
            elif change in {"provider", "application", "grant"}:
                from app.modules.accounts.models import BCAccountAccess
                from app.modules.providers.models import (
                    PromotionLink,
                    ProviderApplication,
                    ProviderConnection,
                )

                link = session.get(PromotionLink, env["link_id"])
                if change == "provider":
                    session.get(
                        ProviderConnection, link.connection_id
                    ).verification_token = uuid4()
                elif change == "application":
                    app = session.exec(
                        select(ProviderApplication).where(
                            ProviderApplication.connection_id == link.connection_id
                        )
                    ).one()
                    app.tiktok_minis_id = "changed-minis"
                else:
                    session.exec(
                        select(BCAccountAccess).where(
                            BCAccountAccess.connection_id == env["connection_id"]
                        )
                    ).one().active = False
            else:
                session.get(
                    TenantMembership,
                    (env["context"].tenant_id, env["context"].actor_id),
                ).role = "viewer"
        return responses(env)[0]

    wire[1].append(remote)
    result = run(env, redis_client, job.id)
    with Session(engine) as session:
        assert not session.exec(
            select(SceneJobPage).where(SceneJobPage.job_id == job.id)
        ).all()
    assert result.status != "COMPLETE"


@pytest.mark.parametrize("published", [False, True])
def test_repair_keeps_current_dispatch_identity_and_broker_backoff(
    job_env, wire, published
):
    from app.modules.builds.scene_jobs import repair_scene_jobs

    env = job_env
    receipt = ensure(env)
    old = datetime.now(UTC) - timedelta(minutes=10)
    retry = datetime.now(UTC) + timedelta(minutes=5)
    with Session(engine) as session, session.begin():
        job = session.get(SceneJob, receipt.job_id)
        job.repair_after = old
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.published_at = old if published else None
        dispatch.available_at = retry
        identity, revision, payload = dispatch.id, job.revision, dict(dispatch.payload)
    assert repair_scene_jobs(database_engine=engine) == 1
    with Session(engine) as session:
        job = session.get(SceneJob, receipt.job_id)
        dispatch = session.get(PendingDispatch, identity)
        assert (
            job.dispatch_id == identity
            and job.revision == revision
            and dispatch.payload == payload
        )
        assert dispatch.published_at is None
        if not published:
            assert dispatch.available_at == retry
    assert not wire[0]


def test_repair_does_not_redeliver_live_claim_or_invalid_identity(job_env, wire):
    from app.modules.builds.scene_jobs import repair_scene_jobs

    result = ensure(job_env)
    with Session(engine) as session, session.begin():
        job = session.get(SceneJob, result.job_id)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        job.claim_token = uuid4()
        job.claimed_until = datetime.now(UTC) + timedelta(seconds=30)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.published_at = datetime.now(UTC)
    assert repair_scene_jobs(database_engine=engine) == 0
    with Session(engine) as session, session.begin():
        job = session.get(SceneJob, result.job_id)
        job.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        job.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        dispatch.payload = {"job_id": str(job.id), "revision": 999}
    assert repair_scene_jobs(database_engine=engine) == 1
    with Session(engine) as session:
        assert session.get(SceneJob, result.job_id).status == "BLOCKED"
    assert not wire[0]


def test_expired_bc_proof_refreshes_without_repeating_shared_scene_gets(
    job_env, wire, redis_client
):
    env = job_env
    job = complete(env, wire, redis_client)
    with Session(engine) as session, session.begin():
        cap = session.get(CapabilityJob, job.capability_job_id)
        cap.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = ensure(env)
    assert result.state == "queued" and result.job_id == job.id
    with Session(engine) as session:
        cap = session.exec(
            select(CapabilityJob).where(CapabilityJob.status == "PENDING")
        ).one()
    wire[1].append(role_page(["actual-account"]))
    run_capability(env, redis_client, cap.id)
    run_capability(env, redis_client, cap.id)
    assert ensure(env).state == "ready"
    assert len(wire[0]) == 7


@pytest.mark.parametrize("expired_proof", [False, True])
def test_restored_actor_can_resume_with_new_job_and_old_receipt_stays_fenced(
    job_env, wire, redis_client, expired_proof
):
    from app.modules.tenants.models import TenantMembership

    env = job_env
    job = ready_for_get(env, wire, redis_client)
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (env["context"].tenant_id, env["context"].actor_id)
        ).role = "viewer"
    blocked = run(env, redis_client, job.id)
    assert blocked.status == "BLOCKED" and blocked.error_code == "action_forbidden"
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (env["context"].tenant_id, env["context"].actor_id)
        ).role = "operator"
    if expired_proof:
        with Session(engine) as session, session.begin():
            session.get(CapabilityJob, job.capability_job_id).expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    renewed = ensure(env)
    assert renewed.state == "queued" and renewed.job_id != job.id
    if expired_proof:
        waiting = run(env, redis_client, renewed.job_id)
        wire[1].append(role_page(["actual-account"]))
        run_capability(env, redis_client, waiting.capability_job_id)
        run_capability(env, redis_client, waiting.capability_job_id)
    wire[1].extend(responses(env))
    for _ in range(5):
        result = run(env, redis_client, renewed.job_id)
    assert result.status == "COMPLETE"
    assert run(env, redis_client, job.id).status == "BLOCKED"
    assert len(wire[0]) == (7 if expired_proof else 6)


def test_restored_authority_bootstraps_again_when_previous_capability_worker_was_blocked(
    job_env, wire, redis_client
):
    env = job_env
    receipt = ensure(env)
    job = run(env, redis_client, receipt.job_id)
    old_cap = job.capability_job_id
    with Session(engine) as session, session.begin():
        cap = session.get(CapabilityJob, old_cap)
        cap.status, cap.error_code = "BLOCKED", "action_forbidden"
    job = run(env, redis_client, job.id)
    assert job.status == "PENDING" and job.capability_job_id != old_cap
    wire[1].append(role_page(["actual-account"]))
    run_capability(env, redis_client, job.capability_job_id)
    run_capability(env, redis_client, job.capability_job_id)
    wire[1].extend(responses(env))
    for _ in range(5):
        job = run(env, redis_client, job.id)
    assert job.status == "COMPLETE" and ensure(env).state == "ready"
