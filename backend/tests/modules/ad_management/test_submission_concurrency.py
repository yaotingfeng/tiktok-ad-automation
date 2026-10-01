"""C4 atomic submission and shared Redis mutation lease checks.

These tests deliberately use the repository PostgreSQL engine and the real
Redis fixture.  Only provider transports are replaced elsewhere in the suite;
there is no provider call in either test here.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.object_coordination import claim_mutation
from app.jobs.models import PendingDispatch
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementTask,
)
from app.modules.ad_management.submissions import submit_management_task


def _preview(session, management_env, *, expires_at=None, created_at=None):
    context, bc, account, route, selection = management_env
    row = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="d" * 64,
        mutation={"field": "status", "mode": "set", "value": "DISABLE"},
        route=route.model_dump(mode="json"),
        created_at=created_at or datetime.now(UTC),
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        status="READY",
        counts={"selected": 1, "targets": 0, "linked": 0, "unsupported": 1},
    )
    session.add(row)
    session.flush()
    session.add(
        ManagementPreviewItem(
            tenant_id=context.tenant_id,
            preview_id=row.id,
            position=0,
            ref={
                "tenant_id": str(context.tenant_id),
                "advertiser_id": account.advertiser_id,
                "kind": "ad",
                "remote_id": "ordinary-ad",
            },
            execution_result="UNSUPPORTED",
            reason="missing_ad_material_id",
        )
    )
    session.flush()
    return row


def test_duplicate_submission_creates_one_task_and_dispatch(session, management_env):
    context, *_ = management_env
    preview = _preview(session, management_env)
    key = uuid4()
    # The shared fixture intentionally owns an uncommitted outer transaction.
    # Commit this self-contained setup before opening the two independent
    # PostgreSQL sessions used by the race, then remove only rows created here.
    session.commit()
    # ``session`` is a savepoint session around the fixture's root PG
    # connection.  Commit that root transaction so independent worker
    # connections can observe the setup rows.
    session.connection().commit()

    def submit_once():
        with Session(engine) as worker:
            result = submit_management_task(
                worker, context, preview.id, preview.digest, key
            )
            worker.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _n: submit_once(), (1, 2))
        assert first.task_id == second.task_id
        with Session(engine) as check:
            assert len(
                check.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_key == f"ad-management:{first.task_id}",
                    )
                ).all()
            ) == 1
    finally:
        # Child rows are explicitly cleaned because setup was committed for
        # visibility across PostgreSQL connections.
        with Session(engine) as cleanup:
            tasks = cleanup.exec(
                select(ManagementTask).where(
                    ManagementTask.tenant_id == context.tenant_id,
                    ManagementTask.actor_id == context.actor_id,
                    ManagementTask.idempotency_key == key,
                )
            ).all()
            for task in tasks:
                cleanup.execute(
                    delete(PendingDispatch).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_key == f"ad-management:{task.id}",
                    )
                )
                cleanup.execute(
                    delete(ManagementTask).where(
                        ManagementTask.tenant_id == context.tenant_id,
                        ManagementTask.id == task.id,
                    )
                )
            cleanup.execute(
                delete(ManagementPreviewItem).where(
                    ManagementPreviewItem.tenant_id == context.tenant_id,
                    ManagementPreviewItem.preview_id == preview.id,
                )
            )
            cleanup.execute(
                delete(ManagementPreview).where(
                    ManagementPreview.tenant_id == context.tenant_id,
                    ManagementPreview.id == preview.id,
                )
            )
            cleanup.commit()


def test_expired_preview_and_failed_outbox_leave_no_task(session, management_env, monkeypatch):
    context, *_ = management_env
    expired = _preview(
        session,
        management_env,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(DomainError, match="preview_expired"):
        submit_management_task(session, context, expired.id, expired.digest, uuid4())
    assert session.exec(select(PendingDispatch)).all() == []

    ready = _preview(session, management_env)
    from app.modules.ad_management import submissions

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(submissions, "enqueue_after_commit", fail_enqueue)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        submit_management_task(session, context, ready.id, ready.digest, uuid4())
    session.rollback()
    assert session.exec(select(PendingDispatch)).all() == []


def test_repeated_lease_is_rejected_and_non_intersecting_objects_run_in_parallel(
    redis_client,
):
    tenant_id = uuid4()
    refs = (
        EntityRef(tenant_id, "parallel-account", "campaign", "series-a"),
        EntityRef(tenant_id, "parallel-account", "campaign", "series-b"),
    )
    owner_a, owner_b = uuid4(), uuid4()
    with Session(engine) as session:
        lease = claim_mutation(session, (refs[0],), owner_a, redis_client=redis_client)
        try:
            with pytest.raises(DomainError, match="mutation_busy"):
                claim_mutation(session, (refs[0],), owner_a, redis_client=redis_client)
        finally:
            lease.release(redis_client)

    def acquire(ref):
        with Session(engine) as worker_session:
            lease = claim_mutation(
                worker_session,
                (ref,),
                owner_a if ref == refs[0] else owner_b,
                redis_client=redis_client,
            )
            lease.assert_current(redis_client)
            return lease

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.map(acquire, refs)
    try:
        assert left.keys != right.keys
    finally:
        left.release(redis_client)
        right.release(redis_client)


def test_cross_bc_object_lease_is_shared_and_stale_generation_is_fenced(
    redis_client,
):
    ref = EntityRef(uuid4(), "advertiser-shared", "campaign", "series-shared")
    owner_a, owner_b = uuid4(), uuid4()
    with Session(engine) as session_a, Session(engine) as session_b:
        lease_a = claim_mutation(session_a, (ref,), owner_a, redis_client=redis_client)
        try:
            with pytest.raises(DomainError, match="mutation_busy"):
                # BC/connection are intentionally absent from EntityRef: the
                # lock domain is the provider object identity.
                claim_mutation(session_b, (ref,), owner_b, redis_client=redis_client)
            lease_a.release(redis_client)
            lease_b = claim_mutation(
                session_b, (ref,), owner_b, redis_client=redis_client
            )
            try:
                assert lease_b.generation > lease_a.generation
                with pytest.raises(DomainError, match="mutation_lease_stale"):
                    lease_a.assert_current(redis_client)
            finally:
                lease_b.release(redis_client)
        finally:
            # Idempotent release also cleans up if an assertion fails.
            lease_a.release(redis_client)


def test_two_postgres_sessions_race_for_one_remote_object(redis_client):
    ref = EntityRef(uuid4(), "race-account", "adgroup", "same-group")
    barrier = Barrier(2)
    settled = Barrier(2)

    def race(owner_id):
        with Session(engine) as worker_session:
            barrier.wait(timeout=5)
            try:
                lease = claim_mutation(
                    worker_session, (ref,), owner_id, redis_client=redis_client
                )
            except DomainError as error:
                settled.wait(timeout=5)
                return error.code
            try:
                result = "acquired"
            finally:
                settled.wait(timeout=5)
                lease.release(redis_client)
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(race, (uuid4(), uuid4())))
    assert sorted(result) == ["acquired", "mutation_busy"]


def test_group_isolation_and_management_share_remote_object_domain(redis_client):
    ref = EntityRef(uuid4(), "shared-account", "adgroup", "isolated-group")
    with Session(engine) as isolation_session, Session(engine) as management_session:
        isolation_lease = claim_mutation(
            isolation_session, (ref,), uuid4(), redis_client=redis_client
        )
        try:
            with pytest.raises(DomainError, match="mutation_busy"):
                claim_mutation(
                    management_session,
                    (ref,),
                    uuid4(),
                    redis_client=redis_client,
                )
        finally:
            isolation_lease.release(redis_client)
