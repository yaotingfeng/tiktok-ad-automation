from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.core.security import create_access_token
from app.models import User
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from app.modules.tenants.permissions import require_tenant
from app.modules.tenants.service import create_tenant, set_member


def user(session, *, platform=False):
    result = User(
        email=f"{uuid4()}@example.com", hashed_password="unused", is_superuser=platform
    )
    session.add(result)
    session.flush()
    return result


def headers(actor_id):
    from datetime import timedelta

    return {
        "Authorization": "Bearer " + create_access_token(actor_id, timedelta(minutes=5))
    }


def test_platform_creates_tenant_and_keeps_actor(session):
    platform, admin = user(session, platform=True), user(session)
    tenant = create_tenant(
        session, actor_id=platform.id, name="  demo  ", administrator_id=admin.id
    )
    assert tenant.name == "demo"
    assert session.get(TenantMembership, (tenant.id, admin.id)).role == "tenant_admin"
    event = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
    ).one()
    assert event.actor_id == platform.id
    assert event.action == "tenant.create"
    with Session(engine) as external:
        assert external.get(Tenant, tenant.id) is None
    context = require_tenant(
        session, actor_id=admin.id, tenant_id=tenant.id, action="manage"
    )
    with pytest.raises(DomainError) as caught:
        set_member(
            session, context=context, user_id=admin.id, role="viewer", active=True
        )
    assert caught.value.code == "last_tenant_admin"


def test_service_does_not_trust_old_platform_flags(session):
    platform, admin = user(session, platform=True), user(session)
    session.execute(
        update(User)
        .where(User.id == platform.id)
        .values(is_superuser=False)
        .execution_options(synchronize_session=False)
    )
    assert platform.is_superuser
    with pytest.raises(DomainError) as caught:
        create_tenant(
            session, actor_id=platform.id, name="demo", administrator_id=admin.id
        )
    assert caught.value.code == "platform_forbidden"


def test_member_write_reloads_context_and_preserves_platform_actor(session, context):
    platform = user(session, platform=True)
    forged = TenantContext(
        tenant_id=context.tenant_id, actor_id=context.actor_id, role="platform_admin"
    )
    with pytest.raises(DomainError) as caught:
        set_member(
            session,
            context=forged,
            user_id=context.actor_id,
            role="viewer",
            active=True,
        )
    assert caught.value.code == "action_forbidden"
    platform_context = TenantContext(
        tenant_id=context.tenant_id, actor_id=platform.id, role="viewer"
    )
    set_member(
        session,
        context=platform_context,
        user_id=context.actor_id,
        role="viewer",
        active=True,
    )
    event = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == context.tenant_id)
    ).one()
    assert event.actor_id == platform.id
    assert event.details == {"role": "viewer", "active": True}


@pytest.mark.parametrize("name", [" ", "x" * 121])
def test_invalid_names_rejected_without_creating_tenant(session, name):
    platform, admin = user(session, platform=True), user(session)
    with pytest.raises(DomainError) as caught:
        create_tenant(
            session, actor_id=platform.id, name=name, administrator_id=admin.id
        )
    assert caught.value.code == "invalid_tenant"


def test_tenant_api_permissions_and_safe_response(client, session, context):
    admin = user(session)
    body = {"name": "Demo", "administrator_id": str(admin.id)}
    denied = client.post(
        "/api/platform/tenants", json=body, headers=headers(context.actor_id)
    )
    assert denied.status_code == 403
    platform = user(session, platform=True)
    result = client.post(
        "/api/platform/tenants", json=body, headers=headers(platform.id)
    )
    assert result.status_code == 201
    assert set(result.json()) == {"id", "name", "active"}
    assert "hashed_password" not in result.text
    tenant_id = result.json()["id"]
    members = client.get(f"/api/tenants/{tenant_id}/members", headers=headers(admin.id))
    assert members.status_code == 200
    assert members.json()["items"][0]["user_id"] == str(admin.id)
    assert "hashed_password" not in members.text


def test_member_endpoint_scopes_permission_and_audit(
    client, session, context, other_context
):
    platform = user(session, platform=True)
    path = f"/api/tenants/{context.tenant_id}/members"
    body = {"user_id": str(other_context.actor_id), "role": "viewer", "active": True}
    assert (
        client.put(path, json=body, headers=headers(context.actor_id)).status_code
        == 403
    )
    response = client.put(path, json=body, headers=headers(platform.id))
    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(context.tenant_id)
    event = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == context.tenant_id)
    ).one()
    assert event.actor_id == platform.id
    assert (
        session.get(
            TenantMembership, (other_context.tenant_id, other_context.actor_id)
        ).role
        == "operator"
    )


def test_inactive_tenants_can_be_listed_and_reactivated_by_platform(
    client, session, context
):
    platform = user(session, platform=True)
    auth = headers(platform.id)
    path = f"/api/platform/tenants/{context.tenant_id}"
    assert client.patch(path, json={"active": False}, headers=auth).status_code == 200
    me = client.get("/api/me/tenants", headers=headers(context.actor_id)).json()
    assert me["items"] == []
    listing = client.get(
        "/api/platform/tenants", params={"active": "false"}, headers=auth
    )
    assert [item["id"] for item in listing.json()["items"]] == [str(context.tenant_id)]
    assert client.patch(path, json={"active": True}, headers=auth).status_code == 200
    assert (
        client.get("/api/me/tenants", headers=headers(context.actor_id)).json()[
            "items"
        ][0]["role"]
        == "operator"
    )


def test_seek_pagination_and_literal_search(client, session):
    platform, admin = user(session, platform=True), user(session)
    tenant_ids = [
        create_tenant(
            session, actor_id=platform.id, name=name, administrator_id=admin.id
        ).id
        for name in ("scope_alpha", "scope_beta", "other")
    ]
    auth = headers(admin.id)
    first = client.get("/api/me/tenants", params={"limit": 2}, headers=auth).json()
    assert [item["id"] for item in first["items"]] == [
        str(value) for value in sorted(tenant_ids)[:2]
    ]
    second = client.get(
        "/api/me/tenants",
        params={"limit": 2, "after_id": first["next_cursor"]},
        headers=auth,
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    filtered = client.get(
        "/api/me/tenants", params={"search": "scope_"}, headers=auth
    ).json()
    assert len(filtered["items"]) == 2
    assert (
        client.get("/api/me/tenants", params={"search": "%"}, headers=auth).json()[
            "items"
        ]
        == []
    )
    assert (
        client.get("/api/me/tenants", params={"limit": 201}, headers=auth).status_code
        == 422
    )


def test_inactive_other_admin_does_not_count_as_replacement(session):
    platform, first, second = user(session, platform=True), user(session), user(session)
    tenant = create_tenant(
        session, actor_id=platform.id, name="Admins", administrator_id=first.id
    )
    context = require_tenant(
        session, actor_id=platform.id, tenant_id=tenant.id, action="manage"
    )
    set_member(
        session, context=context, user_id=second.id, role="tenant_admin", active=True
    )
    second.is_active = False
    session.flush()
    with pytest.raises(DomainError) as caught:
        set_member(
            session, context=context, user_id=first.id, role="viewer", active=True
        )
    assert caught.value.code == "last_tenant_admin"


def test_members_list_filters_seek_and_excludes_secrets(client, session):
    platform, admin = user(session, platform=True), user(session)
    tenant = create_tenant(
        session, actor_id=platform.id, name="Members", administrator_id=admin.id
    )
    context = require_tenant(
        session, actor_id=admin.id, tenant_id=tenant.id, action="manage"
    )
    members = [user(session), user(session), user(session)]
    for member in members:
        set_member(
            session, context=context, user_id=member.id, role="viewer", active=True
        )
    path = f"/api/tenants/{tenant.id}/members"
    auth = headers(admin.id)
    first = client.get(path, params={"role": "viewer", "limit": 2}, headers=auth).json()
    second = client.get(
        path,
        params={"role": "viewer", "limit": 2, "after_id": first["next_cursor"]},
        headers=auth,
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert len({member["user_id"] for member in first["items"] + second["items"]}) == 3
    assert second["next_cursor"] is None
    target = members[0]
    filtered = client.get(path, params={"search": target.email}, headers=auth).json()
    assert [item["user_id"] for item in filtered["items"]] == [str(target.id)]
    assert set(filtered["items"][0]) == {
        "tenant_id",
        "user_id",
        "role",
        "active",
        "email",
        "full_name",
        "user_active",
    }


def test_api_refuses_cross_tenant_reads_and_invalid_member_role(
    client, session, context, other_context
):
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/members",
            headers=headers(context.actor_id),
        ).status_code
        == 403
    )
    platform = user(session, platform=True)
    response = client.put(
        f"/api/tenants/{context.tenant_id}/members",
        json={
            "user_id": str(context.actor_id),
            "role": "platform_admin",
            "active": True,
        },
        headers=headers(platform.id),
    )
    assert response.status_code == 422
    assert (
        session.get(TenantMembership, (context.tenant_id, context.actor_id)).role
        == "operator"
    )


def test_tenant_changes_only_platform_and_have_audit(client, session, context):
    path = f"/api/platform/tenants/{context.tenant_id}"
    assert (
        client.patch(
            path, json={"name": "Forbidden"}, headers=headers(context.actor_id)
        ).status_code
        == 403
    )
    platform = user(session, platform=True)
    auth = headers(platform.id)
    response = client.patch(path, json={"name": " Renamed "}, headers=auth)
    assert response.status_code == 200 and response.json()["name"] == "Renamed"
    assert client.patch(path, json={}, headers=auth).status_code == 422
    assert client.patch(path, json={"name": None}, headers=auth).status_code == 422
    assert (
        client.patch(
            f"/api/platform/tenants/{uuid4()}", json={"active": False}, headers=auth
        ).status_code
        == 404
    )
    event = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == context.tenant_id)
    ).one()
    assert event.details == {"name": "Renamed"}
    assert event.actor_id == platform.id
