from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.core.context import TenantContext
from app.models import User
from app.modules.tenants.models import TenantMembership


def create_context(session: Session, *, role: str = "operator") -> TenantContext:
    user = User(username=f"{uuid4()}", hashed_password="unused")
    tenant_id = uuid4()
    session.add(user)
    session.flush()
    # 历史迁移测试也复用此夹具，只写最初就存在的列，不能依赖今日 ORM 的新增列。
    session.execute(
        text("INSERT INTO tenant (id,name,active) VALUES (:id,:name,true)"),
        {"id": tenant_id, "name": f"test-{uuid4()}"},
    )
    session.add(TenantMembership(tenant_id=tenant_id, user_id=user.id, role=role))
    session.flush()
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role=role)


@pytest.fixture
def context(session: Session) -> TenantContext:
    return create_context(session)


@pytest.fixture
def other_context(session: Session) -> TenantContext:
    return create_context(session)
