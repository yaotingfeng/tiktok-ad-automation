"""Real PostgreSQL capacity reservations, including concurrent final-byte claims."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import delete
from sqlmodel import Session, SQLModel

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.materials.ingest_models import (
    IngestSession,
    ObjectBudget,
    ObjectCleanup,
)
from app.modules.materials.object_budget import (
    mark_object_stored,
    release_object_reservation,
    reserve_object,
)
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context
from tests.modules.materials.test_ingest_models import ingest_fixture, original


def budget_fixture(session, context):
    batch, _, files = ingest_fixture(session, context)
    obj = original(session, context, files[0])
    files[0].current_object_generation = obj.generation
    session.flush()
    return batch, obj


def test_reservation_is_idempotent_and_stored_is_subset(session, context, monkeypatch):
    batch, obj = budget_fixture(session, context)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_GLOBAL_BYTES", 8_000_000_000)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", 5_000_000_000)
    for _ in range(2):
        assert reserve_object(
            session, context=context, object_id=obj.id, byte_size=obj.expected_bytes
        )
        mark_object_stored(
            session, context=context, object_id=obj.id, actual_bytes=obj.expected_bytes
        )
    session.refresh(batch)
    assert (batch.reserved_bytes, batch.stored_bytes) == (obj.expected_bytes,) * 2
    for key in ("global", f"tenant:{context.tenant_id}"):
        budget = session.get(ObjectBudget, key)
        assert budget.reserved_bytes >= obj.expected_bytes
        assert budget.stored_bytes >= obj.expected_bytes
    with pytest.raises(DomainError, match="预留"):
        reserve_object(session, context=context, object_id=obj.id, byte_size=1)


def test_release_requires_exact_confirmed_cleanup_and_is_once(
    session, context, monkeypatch
):
    batch, obj = budget_fixture(session, context)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_GLOBAL_BYTES", 8_000_000_000)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", 5_000_000_000)
    assert reserve_object(
        session, context=context, object_id=obj.id, byte_size=obj.expected_bytes
    )
    mark_object_stored(
        session, context=context, object_id=obj.id, actual_bytes=obj.expected_bytes
    )
    cleanup = ObjectCleanup(
        tenant_id=obj.tenant_id,
        bc_id=obj.bc_id,
        material_id=obj.material_id,
        generation=obj.generation,
        reason="verified_source",
    )
    session.add(cleanup)
    session.flush()
    with pytest.raises(DomainError):
        release_object_reservation(
            session, object_id=obj.id, deletion_evidence_id=cleanup.id
        )
    assert obj.reserved_bytes > 0
    now = datetime.now(UTC)
    cleanup.status = "deleted"
    cleanup.head_confirmed_at = now
    cleanup.delete_confirmed_at = now
    obj.status = "deleted"
    obj.deleted_at = now
    session.flush()
    for _ in range(2):
        release_object_reservation(
            session, object_id=obj.id, deletion_evidence_id=cleanup.id
        )
    session.refresh(batch)
    assert (batch.reserved_bytes, batch.stored_bytes, obj.reserved_bytes) == (0, 0, 0)
    assert obj.reservation_released_at is not None


def test_scope_and_failed_budget_do_not_charge_any_counter(
    session, context, other_context, monkeypatch
):
    batch, obj = budget_fixture(session, context)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", 1)
    assert not reserve_object(
        session, context=context, object_id=obj.id, byte_size=obj.expected_bytes
    )
    with pytest.raises(DomainError):
        reserve_object(
            session,
            context=other_context,
            object_id=obj.id,
            byte_size=obj.expected_bytes,
        )
    session.refresh(batch)
    assert batch.reserved_bytes == obj.reserved_bytes == 0


def test_concurrent_last_capacity_claim_is_atomic(monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_GLOBAL_BYTES", 6_000_000_000)
    monkeypatch.setattr(settings, "MATERIAL_STORAGE_TENANT_BYTES", 6_000_000_000)
    with Session(engine) as setup:
        owner = create_context(setup)
        first_batch, first = budget_fixture(setup, owner)
        second_batch, second = budget_fixture(setup, owner)
        ids = [first.id, second.id]
        batch_ids = (first_batch.id, second_batch.id)
        size = first.expected_bytes
        setup.commit()
    barrier = Barrier(2)

    def run(identity):
        with Session(engine) as worker, worker.begin():
            barrier.wait(timeout=10)
            return reserve_object(
                worker, context=owner, object_id=identity, byte_size=size
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, ids))
        assert sorted(outcomes) == [False, True]
        with Session(engine) as verify:
            budget = verify.get(ObjectBudget, f"tenant:{owner.tenant_id}")
            assert budget.reserved_bytes == size
            assert (
                sum(verify.get(IngestSession, x).reserved_bytes for x in batch_ids)
                == size
            )
    finally:
        with Session(engine) as cleanup, cleanup.begin():
            budget = cleanup.get(ObjectBudget, f"tenant:{owner.tenant_id}")
            global_budget = cleanup.get(ObjectBudget, "global")
            if budget and global_budget:
                global_budget.reserved_bytes -= budget.reserved_bytes
                global_budget.stored_bytes -= budget.stored_bytes
                cleanup.flush()
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c:
                    cleanup.execute(
                        delete(table).where(table.c.tenant_id == owner.tenant_id)
                    )
            cleanup.execute(delete(Tenant).where(Tenant.id == owner.tenant_id))
            cleanup.execute(delete(User).where(User.id == owner.actor_id))
