"""Measured local scheduling and quota boundaries; transports never reach a network."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from time import perf_counter, sleep
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from redis import Redis
from sqlalchemy import func, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.jobs import outbox
from app.jobs.admission import (
    admission_keys,
    admission_policy,
    admit_call,
    release_call,
)
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.modules.accounts.capabilities import (
    ENDPOINT,
    process_capability,
    start_capability_refresh,
)
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.preview_tasks import process_preview
from app.modules.builds.previews import generate_preview
from scripts.benchmark_batches import Parameters, Recorder


def measure_runtime(
    engine: Engine, redis_client: Redis, transport: Any, large_scope: Any
) -> dict[str, Any]:
    from scripts.benchmark_support import bootstrap, prepare, seed_inventory

    policy = admission_policy(ENDPOINT).model_copy(update={"app_max_inflight": 2})
    rendezvous = Barrier(8)
    endpoint = "capacity/concurrent-read"
    owners = [(uuid4(), uuid4()) for _ in range(8)]
    started = perf_counter()

    def worker(number: int) -> bool:
        tenant, lease = owners[number]
        scope: dict[str, Any] = {
            "app_scope": settings.TIKTOK_APP_ID,
            "endpoint": endpoint,
            "tenant_id": tenant,
            "advertiser_id": str(tenant),
        }
        rendezvous.wait(timeout=10)
        result = admit_call(redis_client, **scope, lease_id=lease, policy=policy)
        rendezvous.wait(timeout=10)  # retain both leases until every contender tries
        release_call(redis_client, **scope, lease_id=lease)
        return result.granted

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = sum(pool.map(worker, range(8)))
    keys = admission_keys(
        settings.TIKTOK_APP_ID, endpoint, owners[0][0], str(owners[0][0])
    )
    shared = {
        "workers": 8,
        "worker_kind": "concurrent thread clients, shared Redis Lua",
        "configured_app_inflight": 2,
        "granted": granted,
        "seconds": perf_counter() - started,
        "inflight_after_release": redis_client.zcard(keys[2]),
    }
    assert granted == 2 and shared["inflight_after_release"] == 0

    small_parameters = Parameters(accounts=2, dramas=1, target_accounts=1)
    small = seed_inventory(engine, small_parameters, label="t2")
    transport.scopes[small.bc_id] = small
    auxiliary = Recorder(small_parameters)
    bootstrap(engine, small, 1, redis_client, auxiliary)
    draft_id = prepare(engine, small, small_parameters, transport, auxiliary)
    with Session(engine) as session, session.begin():
        # Earlier task bodies ran synchronously during fixture preparation. Mark
        # precisely this tenant's already-consumed preparation transport records;
        # do not erase the actual T1 backlog used by the fair outbox round.
        SASession.execute(
            session,
            update(PendingDispatch)
            .where(col(PendingDispatch.tenant_id) == small.context.tenant_id)
            .values(published_at=datetime.now(UTC)),
        )
        preview_id = generate_preview(
            session, context=small.context, draft_id=draft_id, expected_revision=1
        )
    with Session(engine) as session:
        backlog = session.exec(
            select(func.count())
            .select_from(PendingDispatch)
            .where(
                PendingDispatch.tenant_id == large_scope.context.tenant_id,
                col(PendingDispatch.published_at).is_(None),
            )
        ).one()
    delivered: list[dict[str, Any]] = []

    def broker(name: str, **kwargs: Any) -> None:
        delivered.append({"name": name, **kwargs})

    started = perf_counter()
    with (
        patch.object(outbox, "engine", engine),
        patch.object(celery_app, "send_task", broker),
    ):
        outbox.flush_dispatch(limit=100)
    small_message = next(
        m for m in delivered if m["kwargs"]["tenant_id"] == str(small.context.tenant_id)
    )
    assert small_message["name"] == "builds.generate_preview"
    process_preview(
        database_engine=engine,
        tenant_id=small.context.tenant_id,
        actor_id=small.context.actor_id,
        payload=small_message["kwargs"]["payload"],
    )
    wait = perf_counter() - started
    with Session(engine) as session:
        units = session.exec(
            select(func.count())
            .select_from(BuildUnit)
            .where(BuildUnit.preview_id == preview_id)
        ).one()
    fairness = {
        "t1_backlog": backlog,
        "first_round_t1_messages": sum(
            m["kwargs"]["tenant_id"] == str(large_scope.context.tenant_id)
            for m in delivered
        ),
        "t2_first_task_seconds": wait,
        "t2_units_after_first_task": units,
        "measurement": "real fair outbox round and first T2 preview task completion; broker transport captured locally",
    }
    assert units > 0 and fairness["first_round_t1_messages"] <= 5

    rate_scope = seed_inventory(engine, small_parameters, label="429")
    with Session(engine) as session, session.begin():
        job_id = start_capability_refresh(
            session,
            context=rate_scope.context,
            bc_id=rate_scope.bc_id,
            connection_id=rate_scope.connection_id,
            request_id=uuid4(),
        )

    def throttled(*_args: Any, **_kwargs: Any) -> HTTPResponse:
        return HTTPResponse(
            status=429,
            body=b'{"code":429,"message":"synthetic rate limit"}',
            headers={"Retry-After": "2"},
        )

    def forbidden_sleep(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("A throttled task must release its worker, never sleep")

    started = perf_counter()
    with (
        patch.object(transport, "sdk", throttled),
        patch("time.sleep", forbidden_sleep),
    ):
        process_capability(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=rate_scope.context.tenant_id,
            actor_id=rate_scope.context.actor_id,
            payload={"job_id": str(job_id), "revision": 0},
        )
    duration = perf_counter() - started
    with Session(engine) as session:
        job = session.get(CapabilityJob, job_id)
        assert (
            job
            and job.status == "PENDING"
            and job.error_code == "capability_remote_unavailable"
        ), (job.status, job.error_code, job.revision) if job else "missing job"
        dispatch = session.get(PendingDispatch, job.dispatch_id)
        assert dispatch and dispatch.payload == {"job_id": str(job_id), "revision": 1}
        delay = (dispatch.available_at - datetime.now(UTC)).total_seconds()
        released = job.claim_token is None and job.claimed_until is None
    keys = admission_keys(
        settings.TIKTOK_APP_ID, ENDPOINT, rate_scope.context.tenant_id, rate_scope.bc_id
    )
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    assert released and delay > 0
    limited = {
        "worker_seconds": duration,
        "due_delay_seconds": delay,
        "claim_released": released,
        "inflight_after_release": 0,
        "next_revision": 1,
        "status": "PENDING",
    }
    # Wait outside the task body for its persisted due time, then consume the
    # exact successor. This measures recovery, not just a scheduled timestamp.
    transport.scopes[rate_scope.bc_id] = rate_scope
    sleep(max(0, delay) + 0.02)
    for _ in range(5):
        with Session(engine) as session:
            job = session.get(CapabilityJob, job_id)
            assert job
            if job.status == "COMPLETE":
                break
            revision = job.revision
        process_capability(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=rate_scope.context.tenant_id,
            actor_id=rate_scope.context.actor_id,
            payload={"job_id": str(job_id), "revision": revision},
        )
    else:
        raise AssertionError("Rate-limited capability did not recover")
    limited["recovery_seconds"] = perf_counter() - started
    limited["recovered_status"] = "COMPLETE"
    return {"shared_quota": shared, "fairness": fairness, "rate_limit": limited}
