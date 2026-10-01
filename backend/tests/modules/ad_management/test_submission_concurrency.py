"""C4 atomic submission and shared Redis mutation lease checks.

These tests deliberately use the repository PostgreSQL engine and the real
Redis fixture.  Only provider transports are replaced elsewhere in the suite;
there is no provider call in either test here.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.object_coordination import claim_mutation
from app.jobs.models import PendingDispatch
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.submissions import submit_management_task
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.conftest import create_context


@pytest.fixture
def committed_management_env():
    """Committed setup visible to two independent PostgreSQL connections."""
    with Session(engine, expire_on_commit=False) as setup:
        context = create_context(setup)
        connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        bc = TenantBC(tenant_id=context.tenant_id, bc_id="management-race-bc")
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="management-race-account",
            currency="USD",
            timezone="UTC",
            remote_status="STATUS_ENABLE",
        )
        setup.add_all([connection, bc, account])
        setup.flush()
        now = datetime.now(UTC)
        setup.add_all(
            [
                BCAccountAccess(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    advertiser_id=account.advertiser_id,
                    connection_id=connection.id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_build=True,
                    can_upload=True,
                    permission_state="VERIFIED",
                    checked_at=now,
                ),
                BCConnectionBinding(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    connection_id=connection.id,
                    kind=connection.kind,
                ),
                ConnectionAuthorization(
                    tenant_id=context.tenant_id,
                    connection_id=connection.id,
                    authorization_revision=connection.authorization_revision,
                    scopes=["read", "build"],
                    permission_summary={"read_authorized": True, "build_authorized": True},
                    source="SYNTHETIC_COMPLETE_EVIDENCE",
                    verified_at=now,
                ),
                BCDefaultRoute(
                    tenant_id=context.tenant_id,
                    bc_id=bc.bc_id,
                    connection_id=connection.id,
                ),
            ]
        )
        selection = FrozenSelectionRecord(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            snapshot_id=uuid4(),
            advertiser_ids=[account.advertiser_id],
            filters={},
            filter_digest="f" * 64,
            publication_versions={},
            naming_versions={},
            refs=[],
            material_uses=[],
            membership_digest="m" * 64,
        )
        setup.add(selection)
        setup.flush()
        route = freeze_route(setup, context=context, bc_id=bc.bc_id)
        setup.commit()
    yield context, bc, account, route, selection


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


def test_duplicate_submission_creates_one_task_and_dispatch(committed_management_env):
    context, *_ = committed_management_env
    with Session(engine, expire_on_commit=False) as setup:
        preview = _preview(setup, committed_management_env)
        setup.commit()
        preview_id, preview_digest = preview.id, preview.digest
    key = uuid4()
    def submit_once():
        with Session(engine) as worker:
            result = submit_management_task(
                worker, context, preview_id, preview_digest, key
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
            copied = check.exec(
                select(ManagementTaskItem).where(
                    ManagementTaskItem.task_id == first.task_id
                )
            ).one()
            assert copied.reason == "missing_ad_material_id"
            assert copied.membership_digest is None
        with Session(engine) as conflict:
            with pytest.raises(DomainError, match="idempotency_conflict"):
                submit_management_task(
                    conflict, context, preview_id, "e" * 64, key
                )
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
                    ManagementPreviewItem.preview_id == preview_id,
                )
            )
            cleanup.execute(
                delete(ManagementPreview).where(
                    ManagementPreview.tenant_id == context.tenant_id,
                    ManagementPreview.id == preview_id,
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

    failed_key = uuid4()
    monkeypatch.setattr(submissions, "enqueue_after_commit", fail_enqueue)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        submit_management_task(session, context, ready.id, ready.digest, failed_key)
    # The nested submission savepoint must remove task/items/outbox even when
    # the caller catches the exception and commits its outer transaction.
    session.commit()
    assert session.exec(select(PendingDispatch)).all() == []
    assert session.exec(
        select(ManagementTask).where(
            ManagementTask.tenant_id == context.tenant_id,
            ManagementTask.idempotency_key == failed_key,
        )
    ).all() == []


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


def test_atomic_fence_rejects_expired_and_replaced_lease(redis_client):
    ref = EntityRef(uuid4(), "fence-account", "campaign", "fence-series")
    with Session(engine) as session:
        old = claim_mutation(
            session, (ref,), uuid4(), redis_client=redis_client, lease_seconds=1
        )
        sleep(1.1)
        with pytest.raises(DomainError, match="mutation_lease_stale"):
            old.fence(redis_client)
        replacement = claim_mutation(
            session, (ref,), uuid4(), redis_client=redis_client, lease_seconds=10
        )
        try:
            with pytest.raises(DomainError, match="mutation_lease_stale"):
                old.fence(redis_client)
            replacement.fence(redis_client)
        finally:
            replacement.release(redis_client)


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
