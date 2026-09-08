from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.models import User
from app.modules.tenants.models import Tenant, TenantMembership
from app.modules.tenants.permissions import require_tenant


def check(session, context, action="read"):
    return require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def test_scope_and_role_are_reloaded(session, context, other_context):
    assert check(session, context, "build").role == "operator"
    with pytest.raises(DomainError, match="无法进入") as cross:
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=other_context.tenant_id,
            action="read",
        )
    assert cross.value.code == "tenant_forbidden"
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    session.execute(
        update(TenantMembership)
        .where(TenantMembership.tenant_id == context.tenant_id)
        .values(active=False)
        .execution_options(synchronize_session=False)
    )
    assert member.active is True  # Deliberately stale identity-map instance.
    with pytest.raises(DomainError) as revoked:
        check(session, context, "build")
    assert revoked.value.code == "tenant_forbidden"


@pytest.mark.parametrize(
    "action", ["build", "upload", "manage", "provider_write", "strategy_write"]
)
def test_viewer_cannot_write(session, context, action):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.flush()
    with pytest.raises(DomainError) as caught:
        check(session, context, action)
    assert caught.value.code == "action_forbidden"
    assert check(session, context).role == "viewer"


def test_platform_actor_preserved_without_membership(session, other_context):
    platform = User(
        email=f"{uuid4()}@example.com", hashed_password="unused", is_superuser=True
    )
    session.add(platform)
    session.flush()
    resolved = require_tenant(
        session,
        actor_id=platform.id,
        tenant_id=other_context.tenant_id,
        action="manage",
    )
    assert resolved.role == "platform_admin"
    assert resolved.actor_id == platform.id
    platform.is_active = False
    session.flush()
    with pytest.raises(DomainError):
        require_tenant(
            session,
            actor_id=platform.id,
            tenant_id=other_context.tenant_id,
            action="read",
        )


def test_platform_role_reloaded_from_database(session, context):
    user = session.get(User, context.actor_id)
    user.is_superuser = True
    session.flush()
    assert check(session, context, "manage").role == "platform_admin"
    session.execute(
        update(User)
        .where(User.id == user.id)
        .values(is_superuser=False)
        .execution_options(synchronize_session=False)
    )
    assert user.is_superuser is True
    with pytest.raises(DomainError) as caught:
        check(session, context, "manage")
    assert caught.value.code == "action_forbidden"


@pytest.mark.parametrize("target", ["user", "tenant"])
def test_inactive_scope_denied(session, context, target):
    if target == "user":
        session.get(User, context.actor_id).is_active = False
    else:
        session.get(Tenant, context.tenant_id).active = False
    session.flush()
    with pytest.raises(DomainError) as caught:
        check(session, context)
    assert caught.value.code == "tenant_forbidden"


def test_unknown_actions_are_never_granted(session, context):
    with pytest.raises(DomainError) as caught:
        check(session, context, "unregistered_action")
    assert caught.value.code == "unknown_action"


def test_database_rejects_platform_role_as_tenant_membership(session, context):
    with pytest.raises(IntegrityError), session.begin_nested():
        member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
        member.role = "platform_admin"
        session.flush()


def test_membership_composite_primary_key_is_unique(session, context):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            TenantMembership(
                tenant_id=context.tenant_id, user_id=context.actor_id, role="viewer"
            )
        )
        session.flush()
