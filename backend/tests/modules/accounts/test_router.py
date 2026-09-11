from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import insert
from sqlmodel import select

from app.core.config import settings
from app.core.security import create_access_token
from app.modules.accounts.models import (
    AdvertiserAccount,
    AuthorizationAttempt,
    BCAccountAccess,
    DiscoveryRun,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.resolver import encode_cursor
from app.modules.tenants.models import AuditEvent, TenantMembership
from tests.modules.accounts.test_access import (
    account_access_case as account_access_case,
)


def test_connection_bc_details_use_latest_complete_snapshot_and_scoped_cursor(
    client, session, account_access_case, other_context
):
    context, grant = account_access_case
    session.add_all(
        [
            TenantBC(tenant_id=context.tenant_id, bc_id="empty-bc", name="Empty BC"),
            TenantBC(
                tenant_id=context.tenant_id, bc_id="unrelated-bc", name="Unrelated"
            ),
        ]
    )
    session.add(
        DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=grant.connection_id,
            status="COMPLETE",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            completed_at=datetime.now(UTC) - timedelta(minutes=30),
            work={"bc_ids": [grant.bc_id, "empty-bc"]},
        )
    )
    session.add(
        DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=grant.connection_id,
            status="ERROR",
            work={"bc_ids": ["unrelated-bc"]},
        )
    )
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/bcs"
    params = {"connection_id": str(grant.connection_id), "limit": 1}
    first = client.get(path, params=params, headers=headers(context))
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(
        path, params={**params, "cursor": cursor}, headers=headers(context)
    )
    assert second.json()["next_cursor"] is None
    assert {row["bc_id"] for row in first.json()["items"] + second.json()["items"]} == {
        grant.bc_id,
        "empty-bc",
    }
    assert (
        client.get(
            path, params={"cursor": cursor}, headers=headers(context)
        ).status_code
        == 422
    )
    assert (
        client.get(
            path, params={"connection_id": str(uuid4())}, headers=headers(context)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/bcs",
            params=params,
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_connection_authorized_time_uses_accepted_completed_candidate_only(
    client, session, account_access_case
):
    context, grant = account_access_case
    accepted = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=grant.connection_id,
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC),
        status="ACCEPTED",
    )
    pending = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=grant.connection_id,
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC),
        status="CANDIDATE_READY",
    )
    session.add_all([accepted, pending])
    session.flush()
    promoted_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(
        DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=grant.connection_id,
            candidate_attempt_id=accepted.id,
            status="COMPLETE",
            completed_at=promoted_at,
        )
    )
    session.add(
        DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=grant.connection_id,
            candidate_attempt_id=pending.id,
            status="ERROR",
            completed_at=datetime.now(UTC),
        )
    )
    session.flush()
    response = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/connections", headers=headers(context)
    )
    assert response.status_code == 200
    value = response.json()["items"][0]["last_authorized_at"]
    assert datetime.fromisoformat(value) == promoted_at


def headers(context):
    return {
        "Authorization": "Bearer "
        + create_access_token(context.actor_id, timedelta(minutes=5))
    }


def test_active_connection_exposes_candidate_discovery_progress(
    client, session, account_access_case
):
    context, grant = account_access_case
    connection = session.get(TikTokConnection, grant.connection_id)
    attempt = AuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=grant.connection_id,
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC),
        status="CANDIDATE_READY",
        base_credential_revision=connection.credential_revision,
    )
    session.add(attempt)
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/tiktok/connections"

    def read():
        result = client.get(path, headers=headers(context))
        assert result.status_code == 200
        item = result.json()["items"][0]
        assert item["status"] == "ACTIVE"
        return item["discovery_status"]

    assert read() == "QUEUED"
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        candidate_attempt_id=attempt.id,
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    assert read() == "RUNNING"
    for state in ("ADMISSION_WAIT", "ERROR"):
        run.status = state
        session.flush()
        assert read() == state
    run.status = "COMPLETE"
    run.completed_at = datetime.now(UTC)
    attempt.status = "ACCEPTED"
    connection.credential_revision += 1
    session.flush()
    assert read() == "COMPLETE"


def test_directory_unique_across_connections_and_safe_fields(
    client, session, account_access_case
):
    context, grant = account_access_case
    second = TikTokConnection(
        tenant_id=context.tenant_id,
        status="ACTIVE",
        credential_ciphertext="sensitive-ciphertext",
    )
    session.add(second)
    session.flush()
    session.add(BCAccountAccess(**{**grant.model_dump(), "connection_id": second.id}))
    session.flush()
    response = client.get(
        f"/api/tenants/{context.tenant_id}/accounts",
        params={"bc_id": grant.bc_id},
        headers=headers(context),
    )
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 1 and page["next_cursor"] is None
    item = page["items"][0]
    assert item["advertiser_id"] == grant.advertiser_id
    assert item["can_build"] and item["availability"] == "AVAILABLE"
    assert "sensitive-ciphertext" not in response.text
    assert "connection_id" not in item


def test_directory_reports_conflict_and_unknown_instead_of_hiding(
    client, session, account_access_case
):
    context, grant = account_access_case
    path = f"/api/tenants/{context.tenant_id}/accounts"
    params = {"bc_id": grant.bc_id}
    grant.permission_state, grant.can_build, grant.can_upload = "UNKNOWN", False, False
    session.flush()
    response = client.get(path, params=params, headers=headers(context))
    assert response.json()["items"][0]["availability"] == "PERMISSION_UNKNOWN"
    session.get(
        AdvertiserAccount, (context.tenant_id, grant.advertiser_id)
    ).ownership_conflict = True
    session.flush()
    response = client.get(
        path,
        params={**params, "availability": "OWNERSHIP_CONFLICT"},
        headers=headers(context),
    )
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["can_build"] is False


def test_account_cursor_rejects_scope_change_and_tampering(
    client, session, account_access_case, other_context
):
    context, grant = account_access_case
    scope = {
        "kind": "accounts",
        "tenant_id": str(context.tenant_id),
        "bc_id": grant.bc_id,
        "query": "",
        "remote_status": None,
        "availability": None,
    }
    cursor = encode_cursor(scope=scope, last_id="1")
    path = f"/api/tenants/{context.tenant_id}/accounts"
    for override in (
        {"query": "other"},
        {"availability": "AVAILABLE"},
        {"remote_status": "STATUS_ENABLE"},
        {"cursor": "bad"},
        {"cursor": cursor[:-3] + "AAA"},
    ):
        response = client.get(
            path,
            params={"bc_id": grant.bc_id, "cursor": cursor, **override},
            headers=headers(context),
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_cursor"
    session.add(TenantBC(tenant_id=other_context.tenant_id, bc_id=grant.bc_id))
    session.flush()
    response = client.get(
        f"/api/tenants/{other_context.tenant_id}/accounts",
        params={"bc_id": grant.bc_id, "cursor": cursor},
        headers=headers(other_context),
    )
    assert response.status_code == 422


def test_resolve_boundaries_and_line_numbers(client, account_access_case):
    context, grant = account_access_case
    path = f"/api/tenants/{context.tenant_id}/accounts/resolve"

    def body(start, count):
        return {
            "bc_id": grant.bc_id,
            "lines": [
                {"line_no": start + index, "raw": "not-found"} for index in range(count)
            ],
        }

    assert (
        client.post(path, json=body(1, 501), headers=headers(context)).status_code
        == 422
    )
    for start in (1, 501):
        response = client.post(path, json=body(start, 500), headers=headers(context))
        assert response.status_code == 200
        assert [row["line_no"] for row in response.json()] == list(
            range(start, start + 500)
        )
    duplicated = {
        "bc_id": grant.bc_id,
        "lines": [{"line_no": 1, "raw": "A"}, {"line_no": 1, "raw": "B"}],
    }
    assert (
        client.post(path, json=duplicated, headers=headers(context)).status_code == 422
    )


def test_tenant_connections_configuration_and_disable_only(
    client, session, account_access_case
):
    context, grant = account_access_case
    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    connection = session.get(TikTokConnection, grant.connection_id)
    connection.credential_ciphertext = "never-expose-this"
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        status="ERROR",
        error_code="arbitrary-sensitive-value",
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/tiktok"
    response = client.get(path + "/connections", headers=headers(context))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == str(connection.id) and item["last_discovery"] is not None
    assert item["error_code"] == "discovery_failed"
    assert (
        "never-expose-this" not in response.text
        and "arbitrary-sensitive-value" not in response.text
    )
    target = path + f"/connections/{connection.id}"
    assert (
        client.patch(
            target, json={"status": "ACTIVE"}, headers=headers(context)
        ).status_code
        == 422
    )
    response = client.patch(
        target, json={"status": "DISABLED"}, headers=headers(context)
    )
    assert response.status_code == 200 and response.json()["status"] == "DISABLED"
    assert (
        client.post(
            path + "/authorizations",
            json={"connection_id": str(connection.id)},
            headers=headers(context),
        ).status_code
        == 409
    )
    audit = session.exec(
        select(AuditEvent).where(AuditEvent.tenant_id == context.tenant_id)
    ).one()
    assert (
        audit.actor_id == context.actor_id
        and audit.action == "tiktok.connection.disable"
    )
    assert connection.credential_ciphertext == "never-expose-this"


def test_generic_disable_cancels_mcp_candidate_and_unpublished_discovery(
    client, session, context
):
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.connection_models import McpAuthorizationAttempt

    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    connection = TikTokConnection(tenant_id=context.tenant_id, kind="OFFICIAL_MCP")
    session.add(connection)
    session.flush()
    attempt = McpAuthorizationAttempt(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        issuer="https://synthetic.invalid/oauth",
        resource="https://synthetic.invalid/mcp",
        redirect_uri="https://synthetic.invalid/callback",
        state_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        status="CANDIDATE_READY",
        candidate_ciphertext="synthetic-encrypted-candidate",
    )
    session.add(attempt)
    session.flush()
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        mcp_candidate_attempt_id=attempt.id,
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    dispatch = PendingDispatch(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        task_name="accounts.mcp_discover",
        task_key=f"synthetic:{run.id}",
        payload={"run_id": str(run.id)},
    )
    session.add(dispatch)
    session.flush()
    revision = connection.authorization_revision
    response = client.patch(
        f"/api/tenants/{context.tenant_id}/tiktok/connections/{connection.id}",
        json={"status": "DISABLED"},
        headers=headers(context),
    )
    assert response.status_code == 200
    session.refresh(connection)
    session.refresh(attempt)
    session.refresh(run)
    assert connection.authorization_revision == revision + 1
    assert attempt.status == "CANCELLED" and attempt.candidate_ciphertext is None
    assert run.status == "CANCELLED"
    assert session.get(PendingDispatch, dispatch.id) is None


def test_authorization_start_commits_state_without_external_exchange(
    client, session, context, sdk_transport
):
    session.get(
        TenantMembership, (context.tenant_id, context.actor_id)
    ).role = "tenant_admin"
    session.flush()
    response = client.post(
        f"/api/tenants/{context.tenant_id}/tiktok/authorizations",
        json={},
        headers=headers(context),
    )
    assert response.status_code == 200
    assert set(response.json()) == {"url"}
    assert response.headers["cache-control"] == "no-store"
    query = parse_qs(urlsplit(response.json()["url"]).query)
    assert query["state"] and query["redirect_uri"]
    assert sdk_transport == []
    attempt = session.exec(
        select(AuthorizationAttempt).where(
            AuthorizationAttempt.tenant_id == context.tenant_id
        )
    ).one()
    assert attempt.status == "PENDING"
    assert settings.TIKTOK_APP_SECRET not in response.text


def test_configuration_readiness_never_exposes_values(client, monkeypatch, context):
    path = f"/api/tenants/{context.tenant_id}/tiktok/configuration"
    for name in (
        "TIKTOK_APP_ID",
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
        "TIKTOK_AUTHORIZATION_URL",
    ):
        monkeypatch.setattr(settings, name, "")
    response = client.get(path, headers=headers(context))
    assert response.status_code == 200
    assert response.json()["status"] == "NOT_CONFIGURED"
    assert set(response.json()["missing_fields"]) == {
        "TIKTOK_APP_ID",
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
        "TIKTOK_AUTHORIZATION_URL",
    }


def test_readonly_and_cross_tenant_cannot_manage_connections(
    client, session, account_access_case, other_context
):
    context, grant = account_access_case
    path = f"/api/tenants/{context.tenant_id}/tiktok"
    assert (
        client.get(path + "/connections", headers=headers(context)).status_code == 200
    )
    assert (
        client.patch(
            path + f"/connections/{grant.connection_id}",
            json={"status": "DISABLED"},
            headers=headers(context),
        ).status_code
        == 403
    )
    session.get(
        TenantMembership, (other_context.tenant_id, other_context.actor_id)
    ).role = "tenant_admin"
    session.flush()
    other = f"/api/tenants/{other_context.tenant_id}/tiktok/connections/{grant.connection_id}"
    assert (
        client.patch(
            other, json={"status": "DISABLED"}, headers=headers(other_context)
        ).status_code
        == 404
    )


def test_10001_directory_accounts_page_exactly_once(
    client, session, account_access_case
):
    context, grant = account_access_case
    count = 10001
    session.execute(
        insert(AdvertiserAccount),
        [
            {
                "tenant_id": context.tenant_id,
                "advertiser_id": f"bulk-{index:05d}",
                "name": f"Scale account {index}",
                "currency": "USD",
                "timezone": "UTC",
                "remote_status": "STATUS_ENABLE",
                "ownership_conflict": False,
            }
            for index in range(count)
        ],
    )
    session.execute(
        insert(BCAccountAccess),
        [
            {
                "tenant_id": context.tenant_id,
                "bc_id": grant.bc_id,
                "advertiser_id": f"bulk-{index:05d}",
                "connection_id": grant.connection_id,
                "in_bc": True,
                "authorized": True,
                "active": True,
                "can_upload": False,
                "can_build": False,
                "permission_state": "UNKNOWN",
            }
            for index in range(count)
        ],
    )
    session.flush()
    path = f"/api/tenants/{context.tenant_id}/accounts"
    params = {"bc_id": grant.bc_id, "query": "Scale account", "limit": 200}
    observed = []
    while True:
        response = client.get(path, params=params, headers=headers(context))
        assert response.status_code == 200
        page = response.json()
        assert 1 <= len(page["items"]) <= 200
        observed.extend(item["advertiser_id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]
        assert len(observed) <= count
    assert len(observed) == len(set(observed)) == count
    assert observed == sorted(observed)


def test_bc_and_connection_pages_bind_filters(client, session, account_access_case):
    context, grant = account_access_case
    for index in range(3):
        session.add(
            TenantBC(
                tenant_id=context.tenant_id,
                bc_id=f"more-{index}",
                name=f"Extra {index}",
            )
        )
        session.add(TikTokConnection(tenant_id=context.tenant_id))
    session.flush()
    root = f"/api/tenants/{context.tenant_id}"
    for suffix, identity in (("/bcs", "bc_id"), ("/tiktok/connections", "id")):
        first = client.get(
            root + suffix, params={"limit": 2}, headers=headers(context)
        ).json()
        second = client.get(
            root + suffix,
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=headers(context),
        ).json()
        assert len(first["items"]) == 2 and len(second["items"]) == 2
        assert second["next_cursor"] is None
        assert len({item[identity] for item in first["items"] + second["items"]}) == 4
        changed = {"query": "Extra"} if suffix == "/bcs" else {"status": "ACTIVE"}
        assert (
            client.get(
                root + suffix,
                params={"cursor": first["next_cursor"], **changed},
                headers=headers(context),
            ).status_code
            == 422
        )
    assert (
        client.get(root + "/bcs", params={"query": "Extra"}, headers=headers(context))
        .json()["items"][0]["name"]
        .startswith("Extra")
    )
    assert (
        client.get(
            root + "/accounts",
            params={"bc_id": grant.bc_id, "limit": 201},
            headers=headers(context),
        ).status_code
        == 422
    )


def test_encryption_missing_is_not_reported_as_missing_app(
    client, monkeypatch, context
):
    monkeypatch.setattr(settings, "CONNECTION_ENCRYPTION_KEY", "")
    response = client.get(
        f"/api/tenants/{context.tenant_id}/tiktok/configuration",
        headers=headers(context),
    )
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "status": "INCOMPLETE",
        "missing_fields": ["CONNECTION_ENCRYPTION_KEY"],
        "code": "connection_encryption_unconfigured",
    }


def test_declared_permission_preserved_when_grant_has_no_business_capability(
    client, session, account_access_case
):
    context, grant = account_access_case
    grant.can_build = grant.can_upload = False
    session.flush()
    result = client.get(
        f"/api/tenants/{context.tenant_id}/accounts",
        params={"bc_id": grant.bc_id},
        headers=headers(context),
    ).json()["items"][0]
    assert result["permission_state"] == "VERIFIED"
    assert result["availability"] == "NO_ACCESS"
