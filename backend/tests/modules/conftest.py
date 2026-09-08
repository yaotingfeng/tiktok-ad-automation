from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.context import TenantContext
from app.models import User
from app.modules.tenants.models import Tenant, TenantMembership


def create_context(session: Session, *, role: str = "operator") -> TenantContext:
    user = User(email=f"{uuid4()}@example.com", hashed_password="unused")
    tenant = Tenant(name=f"test-{uuid4()}")
    session.add_all([user, tenant])
    session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role=role))
    session.flush()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, role=role)


@pytest.fixture
def context(session: Session) -> TenantContext:
    return create_context(session)


@pytest.fixture
def other_context(session: Session) -> TenantContext:
    return create_context(session)
