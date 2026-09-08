from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import Session, delete, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from app.modules.tenants.service import set_member


@pytest.fixture
def committed_tenant():
    """Own committed scenario rows; no test savepoint pretends process visibility."""
    tenant = Tenant(name=f"concurrency-{uuid4()}")
    platform = User(
        email=f"{uuid4()}@example.com", hashed_password="unused", is_superuser=True
    )
    first = User(email=f"{uuid4()}@example.com", hashed_password="unused")
    second = User(email=f"{uuid4()}@example.com", hashed_password="unused")
    ids = [platform.id, first.id, second.id]
    tenant_id = tenant.id
    with Session(engine) as session:
        session.add_all([tenant, platform, first, second])
        session.flush()
        session.add_all(
            [
                TenantMembership(
                    tenant_id=tenant_id, user_id=first.id, role="tenant_admin"
                ),
                TenantMembership(
                    tenant_id=tenant_id, user_id=second.id, role="tenant_admin"
                ),
            ]
        )
        session.commit()
    try:
        yield tenant_id, ids
    finally:
        with Session(engine) as session:
            session.exec(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
            session.exec(
                delete(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
            )
            session.exec(delete(Tenant).where(Tenant.id == tenant_id))
            for user_id in ids:
                session.exec(delete(User).where(User.id == user_id))
            session.commit()


def test_concurrent_removal_preserves_last_admin(committed_tenant):
    tenant_id, (platform_id, first_id, second_id) = committed_tenant
    context = TenantContext(
        tenant_id=tenant_id, actor_id=platform_id, role="platform_admin"
    )
    attempting = Event()

    def remove_second():
        with Session(engine) as session:
            # Cache before waiting to prove the service refreshes stale state.
            session.get(TenantMembership, (tenant_id, first_id))
            attempting.set()
            try:
                set_member(
                    session,
                    context=context,
                    user_id=second_id,
                    role="viewer",
                    active=True,
                )
                session.commit()
                return "changed"
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as first:
            first.exec(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            ).one()
            future = executor.submit(remove_second)
            assert attempting.wait(5)
            # The contender must wait on the actual PostgreSQL tenant lock.
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            set_member(
                first, context=context, user_id=first_id, role="viewer", active=True
            )
            first.commit()
        assert future.result(timeout=5) == "last_tenant_admin"
    with Session(engine) as session:
        members = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
        ).all()
        assert (
            sum(member.active and member.role == "tenant_admin" for member in members)
            == 1
        )
        events = session.exec(
            select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        ).all()
        assert len(events) == 1
        assert events[0].actor_id == platform_id


def test_permission_revoked_while_waiting_for_tenant_lock(committed_tenant):
    tenant_id, (platform_id, first_id, second_id) = committed_tenant
    started = Event()

    def change_as_first_admin():
        with Session(engine) as session:
            started.set()
            context = TenantContext(
                tenant_id=tenant_id, actor_id=first_id, role="tenant_admin"
            )
            try:
                set_member(
                    session,
                    context=context,
                    user_id=second_id,
                    role="viewer",
                    active=True,
                )
                session.commit()
                return "changed"
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as first:
            first.exec(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            ).one()
            future = executor.submit(change_as_first_admin)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            set_member(
                first,
                context=TenantContext(
                    tenant_id=tenant_id, actor_id=platform_id, role="platform_admin"
                ),
                user_id=first_id,
                role="viewer",
                active=True,
            )
            first.commit()
        assert future.result(timeout=5) == "action_forbidden"
