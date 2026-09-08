from uuid import uuid4

import pytest

from app.models import User
from app.modules.tenants.models import TenantMembership
from tests.modules.tenants.test_management import headers, user


def test_managers_search_active_users_without_private_fields(client, session, context):
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "tenant_admin"
    marker = uuid4().hex
    candidates = [
        User(
            email=f"{marker}-{i}@example.com",
            full_name="候选用户",
            hashed_password="private",
            is_active=i < 3,
        )
        for i in range(4)
    ]
    session.add_all(candidates)
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/member-candidates"
    auth = headers(context.actor_id)
    first = client.get(path, params={"query": marker, "limit": 2}, headers=auth)
    assert first.status_code == 200
    data = first.json()
    assert len(data["items"]) == 2 and data["next_cursor"]
    second = client.get(
        path,
        params={"query": marker, "limit": 2, "after_id": data["next_cursor"]},
        headers=auth,
    ).json()
    assert second["next_cursor"] is None
    rows = data["items"] + second["items"]
    assert {row["id"] for row in rows} == {str(item.id) for item in candidates[:3]}
    assert all(set(row) == {"id", "email", "full_name"} for row in rows)
    assert "private" not in first.text
    member.role = "viewer"
    session.flush()
    assert client.get(path, params={"query": marker}, headers=auth).status_code == 403


def test_candidate_search_requires_scoped_management(
    client, session, context, other_context
):
    auth = headers(context.actor_id)
    for path in [
        "/api/platform/user-candidates",
        f"/api/tenants/{other_context.tenant_id}/member-candidates",
        f"/api/tenants/{context.tenant_id}/member-candidates",
    ]:
        assert (
            client.get(path, params={"query": "user"}, headers=auth).status_code == 403
        )
    platform = user(session, platform=True)
    assert client.get(
        "/api/platform/user-candidates",
        params={"query": str(platform.email)},
        headers=headers(platform.id),
    ).json()["items"][0]["id"] == str(platform.id)


@pytest.mark.parametrize("query", ["", " ", "x" * 256])
def test_candidate_search_rejects_empty_or_unbounded_terms(client, session, query):
    platform = user(session, platform=True)
    assert (
        client.get(
            "/api/platform/user-candidates",
            params={"query": query},
            headers=headers(platform.id),
        ).status_code
        == 422
    )


def test_candidate_search_treats_wildcards_as_literal(client, session):
    platform = user(session, platform=True)
    response = client.get(
        "/api/platform/user-candidates",
        params={"query": "%_"},
        headers=headers(platform.id),
    )
    assert response.status_code == 200 and response.json()["items"] == []


def test_candidate_search_accepts_exact_user_id(client, session):
    platform = user(session, platform=True)
    candidate = user(session)
    response = client.get(
        "/api/platform/user-candidates",
        params={"query": str(candidate.id)},
        headers=headers(platform.id),
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(candidate.id)]
    candidate.is_active = False
    session.flush()
    assert client.get(
        "/api/platform/user-candidates",
        params={"query": str(candidate.id)},
        headers=headers(platform.id),
    ).json()["items"] == []
