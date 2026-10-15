"""MCP 候选目录必须完整暂存后才发布；测试不调用真实提供方。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from mcp.types import CallToolResult
from sqlmodel import Session, delete, select

from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol, load_tool_contracts
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    ConnectionToolObservation,
    McpRefreshAttempt,
)
from app.modules.accounts.discovery import publish_mcp_directory
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.mcp_discovery_tasks import process_mcp_discovery
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    ExternalAssetOwner,
    TenantBC,
    TikTokConnection,
)
from tests.modules.accounts.test_mcp_binding import (  # noqa: F401
    app_config,
    bc_page,
    read_bcs,
)
from tests.modules.accounts.test_mcp_binding import (
    candidate as _candidate,
)
from tests.modules.accounts.test_mcp_binding import (
    catalog_wire as _catalog_wire,
)
from tests.modules.accounts.test_mcp_binding import (
    committed_context as _committed_context,
)
from tests.modules.accounts.test_mcp_binding import (
    oauth_wire as _oauth_wire,
)
from tests.modules.conftest import create_context

candidate = _candidate
catalog_wire = _catalog_wire
committed_context = _committed_context
oauth_wire = _oauth_wire


def seed_connection(session, context, bc_ids):
    """只种合成活动授权；所有发现业务仍走真实本地 MCP HTTP。"""
    profile = load_mcp_protocol()
    connection = TikTokConnection(
        tenant_id=context.tenant_id,
        kind="OFFICIAL_MCP",
        status="ACTIVE",
        credential_revision=1,
        authorization_revision=1,
        adapter_contract_revision=profile.schema_manifest_sha256,
        service_profile=profile.revision,
        credential_ciphertext=encrypt_credentials(
            tenant_id=context.tenant_id,
            value={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "issuer": profile.issuer,
                "resource": profile.resource,
                "scopes": '["mcp:tt4b"]',
                "client_id": "synthetic-client",
                "token_type": "Bearer",
                "received_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        ),
    )
    session.add(connection)
    session.flush()
    session.add(
        ConnectionAuthorization(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            authorization_revision=1,
            upstream_subject="synthetic-subject",
            issuer=profile.issuer,
            resource=profile.resource,
            scopes=["mcp:tt4b"],
            permission_summary={
                "read_authorized": None,
                "upload_authorized": None,
                "build_authorized": None,
            },
            source="MCP_AUTHORIZATION_BOOTSTRAP",
        )
    )
    schemas = {
        contract.tool_name: {
            "name": contract.tool_name,
            "inputSchema": contract.input_schema,
            **(
                {"outputSchema": contract.output_schema}
                if contract.output_schema is not None
                else {}
            ),
        }
        for contract in load_tool_contracts()
    }
    observation = ConnectionToolObservation(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        schema_digest=sha256(
            json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        expected_contract_revision=profile.schema_manifest_sha256,
        pagination_complete=True,
        tool_schemas=schemas,
        call_evidence={
            "authorization_revision": 1,
            "business_centers_complete": True,
            "business_centers": [{"bc_id": bc_id, "name": bc_id} for bc_id in bc_ids],
        },
    )
    session.add(observation)
    session.flush()
    return connection, observation


def seed_run(session, context, connection, observation, bc_id):
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id=bc_id))
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=bc_id,
            connection_id=connection.id,
            kind="OFFICIAL_MCP",
            status="SYNCING",
            authorization_revision=1,
            revision=1,
        )
    )
    run = DiscoveryRun(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        connection_id=connection.id,
        bc_id=bc_id,
        authorization_revision=1,
        binding_revision=1,
        work={
            "stage": "MCP_VERIFY",
            "bc_id": bc_id,
            "observation_id": str(observation.id),
            "page": 1,
        },
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def incomplete_mcp_run(session):
    context = create_context(session, role="tenant_admin")
    connection, observation = seed_connection(session, context, ["bc-selected"])
    run = seed_run(session, context, connection, observation, "bc-selected")
    return context, run


def test_publish_rejects_incomplete_scan(session, incomplete_mcp_run):
    context, run = incomplete_mcp_run
    with pytest.raises(DomainError) as error:
        publish_mcp_directory(session, context=context, run_id=run.id)
    assert error.value.code == "discovery_incomplete"
    assert session.get(TikTokConnection, run.connection_id).status == "ACTIVE"


def test_incomplete_scan_preserves_old_active_credential(session, incomplete_mcp_run):
    context, run = incomplete_mcp_run
    connection = session.get(TikTokConnection, run.connection_id)
    connection.status = "ACTIVE"
    connection.credential_ciphertext = "synthetic-old-encrypted-material"
    session.add(connection)
    session.flush()
    with pytest.raises(DomainError):
        publish_mcp_directory(session, context=context, run_id=run.id)
    session.refresh(connection)
    assert connection.status == "ACTIVE"
    assert connection.credential_ciphertext == "synthetic-old-encrypted-material"
    assert connection.credential_revision == 1
    assert connection.authorization_revision == 1


@pytest.fixture
def directory_context(committed_context):
    yield committed_context
    from app.modules.accounts.capability_models import (
        CapabilityAsset,
        CapabilityJob,
        CapabilityPage,
        CapabilityRequest,
    )

    with Session(engine) as own:
        for model in (
            CapabilityAsset,
            CapabilityPage,
            CapabilityRequest,
            CapabilityJob,
            DiscoveryStagedPage,
            BCAccountAccess,
            McpRefreshAttempt,
            ConnectionAuthorization,
            AdvertiserAccount,
        ):
            own.exec(
                delete(model).where(model.tenant_id == committed_context.tenant_id)
            )
        own.exec(
            delete(ExternalAssetOwner).where(
                ExternalAssetOwner.owner_tenant_id == committed_context.tenant_id
            )
        )
        own.commit()


@pytest.fixture
def discovery_scenario(directory_context):
    bc_id = f"bc-{directory_context.tenant_id}"
    with Session(engine) as own:
        connection, observation = seed_connection(own, directory_context, [bc_id])
        run = seed_run(own, directory_context, connection, observation, bc_id)
        run_id = run.id
        own.commit()
    return directory_context, run_id, bc_id


def response(wire, tool, data):
    wire.enqueue_result(
        tool, CallToolResult(content=[], structuredContent={"code": 0, "data": data})
    )


def remote_page(rows, *, page=1, pages=1, total=None):
    return {
        "list": rows,
        "page_info": {
            "page": page,
            "page_size": 50,
            "total_page": pages,
            "total_number": len(rows) if total is None else total,
        },
    }


def enqueue_directory(wire, bc_id, ids, *, role="ADMIN"):
    response(wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    response(
        wire,
        "auth_advertiser_get",
        {"list": [{"advertiser_id": identity} for identity in ids]},
    )
    response(
        wire, "bc_get", remote_page([{"bc_info": {"bc_id": bc_id, "name": "Selected"}}])
    )
    assets = [
        {
            "asset_id": identity,
            "asset_name": "Synthetic",
            "asset_type": "ADVERTISER",
            "advertiser_role": role,
        }
        for identity in ids
    ]
    response(wire, "bc_asset_get", remote_page(assets))
    if ids:
        response(
            wire,
            "advertiser_info_get",
            {
                "list": [
                    {
                        "advertiser_id": identity,
                        "name": "Synthetic",
                        "currency": "USD",
                        "timezone": "UTC",
                        "status": "STATUS_ENABLE",
                    }
                    for identity in ids
                ]
            },
        )
    response(wire, "bc_asset_get", remote_page(assets))


def step(context, run_id, redis_client, *, initial=False):
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        revision = run.revision
    payload = {"run_id": str(run_id)}
    if not initial:
        payload["revision"] = revision
    process_mcp_discovery(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        return run.status, run.work["stage"], run.error_code


def finish(context, run_id, redis_client):
    for _ in range(15):
        state = step(context, run_id, redis_client)
        if state[0] in {"COMPLETE", "ERROR"}:
            return state
    raise AssertionError(f"discovery did not finish: {state}")


@pytest.mark.parametrize("account_role", ["ADMIN", "OPERATOR", "ANALYST"])
def test_complete_http_directory_publishes_once_atomically(
    discovery_scenario, catalog_wire, redis_client, account_role
):
    context, run_id, bc_id = discovery_scenario
    ids = (f"ad-{context.tenant_id}",)
    enqueue_directory(catalog_wire, bc_id, ids, role=account_role)
    assert step(context, run_id, redis_client, initial=True) == (
        "RUNNING",
        "AUTHORIZED",
        None,
    )
    for expected in ("BCS", "ASSETS", "DETAILS", "ROLES", "FINALIZE"):
        assert step(context, run_id, redis_client) == ("RUNNING", expected, None)
        with Session(engine) as own:
            run = own.get(DiscoveryRun, run_id)
            assert own.get(TikTokConnection, run.connection_id).status == "ACTIVE"
            assert not own.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == context.tenant_id
                )
            ).all()
    assert step(context, run_id, redis_client) == ("COMPLETE", "FINALIZE", None)
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        connection = own.get(TikTokConnection, run.connection_id)
        assert connection.status == "ACTIVE"
        assert (connection.credential_revision, connection.authorization_revision) == (
            1,
            1,
        )
        assert run.mcp_candidate_attempt_id is None
        authorization = own.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection.id
            )
        ).one()
        assert authorization.upstream_subject == "synthetic-subject"
        assert authorization.upstream_grant_id is None
        assert authorization.scopes == ["mcp:tt4b"]
        assert authorization.permission_summary == {
            "read_authorized": True,
            "upload_authorized": True,
            "build_authorized": True,
        }
        access = own.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == connection.id
            )
        ).one()
        assert access.active and access.authorized and access.in_bc
        assert access.can_build == access.can_upload == (account_role != "ANALYST")
        assert access.permission_state == "VERIFIED"
        from app.modules.accounts.capabilities import get_capability_evidence

        proof = get_capability_evidence(
            own,
            context=context,
            bc_id=bc_id,
            advertiser_id=ids[0],
            connection_id=connection.id,
        )
        assert proof is not None and proof.scope_verified
        assert proof.can_build == proof.can_upload == (account_role != "ANALYST")
        assert own.get(BCConnectionBinding, (context.tenant_id, bc_id, connection.id))
        assert (
            own.get(BCDefaultRoute, (context.tenant_id, bc_id)).connection_id
            == connection.id
        )
        assert (
            len(
                own.exec(
                    select(DiscoveryStagedPage).where(
                        DiscoveryStagedPage.run_id == run_id
                    )
                ).all()
            )
            == 6
        )
    before = len(catalog_wire.calls)
    assert step(context, run_id, redis_client, initial=True)[0] == "COMPLETE"
    assert len(catalog_wire.calls) == before


def to_finalize(context, run_id, redis_client):
    for _ in range(12):
        status, stage, error = step(context, run_id, redis_client)
        assert status == "RUNNING", (status, stage, error)
        if stage == "FINALIZE":
            return
    raise AssertionError("did not reach FINALIZE")


def old_snapshot(context, run_id, bc_id):
    from app.modules.accounts.models import TenantBC

    old_id = f"old-{context.tenant_id}"
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        connection = own.get(TikTokConnection, run.connection_id)
        connection.status = "ACTIVE"
        own.add(connection)
        old_bc = own.get(TenantBC, (context.tenant_id, bc_id))
        old_bc.name = "Old BC"
        own.add(old_bc)
        own.add(
            AdvertiserAccount(
                tenant_id=context.tenant_id,
                advertiser_id=old_id,
                name="Old account",
                currency="USD",
                timezone="UTC",
                remote_status="STATUS_ENABLE",
            )
        )
        own.flush()
        own.add(
            BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id=bc_id,
                advertiser_id=old_id,
                connection_id=connection.id,
                in_bc=True,
                authorized=True,
                active=True,
                permission_state="UNKNOWN",
            )
        )
        own.commit()
    return old_id


def assert_old_snapshot(_context, run_id, old_id):
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        connection = own.get(TikTokConnection, run.connection_id)
        assert connection.credential_ciphertext
        assert connection.status == "ACTIVE"
        assert (connection.credential_revision, connection.authorization_revision) == (
            1,
            1,
        )
        grant = own.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == connection.id
            )
        ).one()
        assert grant.advertiser_id == old_id and grant.active and grant.authorized


def test_second_asset_page_error_keeps_old_snapshot(
    discovery_scenario, catalog_wire, redis_client
):
    context, run_id, bc_id = discovery_scenario
    old_id = old_snapshot(context, run_id, bc_id)
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    response(catalog_wire, "auth_advertiser_get", {"list": []})
    response(catalog_wire, "bc_get", remote_page([{"bc_info": {"bc_id": bc_id}}]))
    response(
        catalog_wire,
        "bc_asset_get",
        remote_page(
            [
                {"asset_id": f"new-{context.tenant_id}-{i}", "asset_type": "ADVERTISER"}
                for i in range(50)
            ],
            pages=2,
            total=51,
        ),
    )
    catalog_wire.enqueue_result(
        "bc_asset_get",
        CallToolResult(
            content=[],
            structuredContent={"code": 40000, "message": "synthetic-private-error"},
        ),
    )
    state = finish(context, run_id, redis_client)
    assert state[0] == "ERROR" and state[1] == "ASSETS"
    assert_old_snapshot(context, run_id, old_id)
    with Session(engine) as own:
        assert own.get(DiscoveryStagedPage, (run_id, bc_id, "ASSETS", 1))
        assert not own.get(DiscoveryStagedPage, (run_id, bc_id, "ASSETS", 2))
    assert not any(
        call["method"] == "tools/call"
        and call["params"]["name"] == "advertiser_info_get"
        for call in catalog_wire.calls
    )


@pytest.mark.parametrize(
    "extra", [{"has_more": True}, {"page_info": {}}, {"next_cursor": "opaque"}]
)
def test_unpaginated_truncation_marker_fails_closed(
    discovery_scenario, catalog_wire, redis_client, extra
):
    context, run_id, _ = discovery_scenario
    response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
    response(catalog_wire, "auth_advertiser_get", {"list": [], **extra})
    assert step(context, run_id, redis_client)[1] == "AUTHORIZED"
    assert step(context, run_id, redis_client) == (
        "ERROR",
        "AUTHORIZED",
        "unsupported_account_schema",
    )
    with Session(engine) as own:
        assert not own.get(DiscoveryStagedPage, (run_id, "", "AUTHORIZED", 1))


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "scope", "digest", "future", "expired"]
)
def test_invalid_final_evidence_records_error_preserving_old(
    discovery_scenario, catalog_wire, redis_client, damage
):
    context, run_id, bc_id = discovery_scenario
    old_id = old_snapshot(context, run_id, bc_id)
    enqueue_directory(catalog_wire, bc_id, (f"new-{context.tenant_id}",))
    to_finalize(context, run_id, redis_client)
    with Session(engine) as own:
        page = own.get(
            DiscoveryStagedPage,
            (
                run_id,
                "" if damage == "scope" else bc_id,
                "SUBJECT" if damage == "scope" else "DETAILS",
                1,
            ),
        )
        if damage == "missing":
            own.delete(page)
        elif damage == "duplicate":
            page.rows = [page.rows[0], page.rows[0]]
        elif damage == "scope":
            page.rows = [{**page.rows[0], "scopes": []}]
        elif damage == "digest":
            page.schema_digest = "0" * 64
        elif damage == "future":
            page.observed_at = datetime.now(UTC) + timedelta(hours=1)
        else:
            from app.modules.accounts.connection_models import ConnectionToolObservation

            run = own.get(DiscoveryRun, run_id)
            from uuid import UUID

            observation = own.get(
                ConnectionToolObservation, UUID(run.work["observation_id"])
            )
            observation.observed_at = datetime.now(UTC) - timedelta(days=2)
            own.add(observation)
        if damage != "missing":
            own.add(page)
        own.commit()
    before = len(catalog_wire.calls)
    assert step(context, run_id, redis_client) == (
        "ERROR",
        "FINALIZE",
        "discovery_incomplete",
    )
    assert len(catalog_wire.calls) == before
    assert_old_snapshot(context, run_id, old_id)


def test_removed_membership_stops_worker_before_http(
    discovery_scenario, catalog_wire, redis_client
):
    from app.core.context import TenantContext
    from app.jobs.models import PendingDispatch
    from app.models import User
    from app.modules.accounts.connections import sync_connection_bc
    from app.modules.tenants.models import AuditEvent, TenantMembership

    context, run_id, bc_id = discovery_scenario
    with Session(engine) as own:
        member = own.get(TenantMembership, (context.tenant_id, context.actor_id))
        member.active = False
        own.add(member)
        own.commit()
    before = len(catalog_wire.calls)
    with pytest.raises(DomainError) as failure:
        step(context, run_id, redis_client)
    assert failure.value.code == "tenant_forbidden"
    assert len(catalog_wire.calls) == before
    with Session(engine) as own:
        original = own.get(DiscoveryRun, run_id)
        assert original.status == "ERROR" and original.error_code == "tenant_forbidden"
        binding = own.get(
            BCConnectionBinding, (context.tenant_id, bc_id, original.connection_id)
        )
        assert (
            binding.status == "ERROR" and binding.last_error_code == "tenant_forbidden"
        )
        replacement = User(username=f"replacement-{uuid4()}", hashed_password="unused")
        own.add(replacement)
        own.flush()
        replacement_id = replacement.id
        own.add(
            TenantMembership(
                tenant_id=context.tenant_id, user_id=replacement_id, role="tenant_admin"
            )
        )
        own.commit()
        connection_id = original.connection_id
    replacement_context = TenantContext(
        tenant_id=context.tenant_id, actor_id=replacement_id, role="tenant_admin"
    )
    retry_id = None
    try:
        with Session(engine) as own:
            retry_id = sync_connection_bc(
                own,
                context=replacement_context,
                connection_id=connection_id,
                bc_id=bc_id,
            )
            assert retry_id != run_id
            assert own.get(DiscoveryRun, retry_id).actor_id == replacement_id
            own.commit()
        response(catalog_wire, "user_info_get", {"core_user_id": "synthetic-subject"})
        assert step(replacement_context, retry_id, redis_client) == (
            "RUNNING",
            "AUTHORIZED",
            None,
        )
        with Session(engine) as own:
            assert own.get(DiscoveryRun, run_id).status == "ERROR"
    finally:
        # 额外管理员只归本用例所有；父 fixture 随后继续清理原租户其余证据。
        with Session(engine) as own:
            if retry_id is not None:
                own.exec(
                    delete(DiscoveryStagedPage).where(
                        DiscoveryStagedPage.run_id == retry_id
                    )
                )
            for model in (PendingDispatch, AuditEvent, DiscoveryRun):
                own.exec(
                    delete(model).where(
                        model.tenant_id == context.tenant_id,
                        model.actor_id == replacement_id,
                    )
                )
            own.exec(
                delete(TenantMembership).where(
                    TenantMembership.tenant_id == context.tenant_id,
                    TenantMembership.user_id == replacement_id,
                )
            )
            own.exec(delete(User).where(User.id == replacement_id))
            own.commit()


@pytest.mark.parametrize(
    "fence",
    [
        "run_revision",
        "binding_revision",
        "authorization_revision",
        "disabled",
        "claim",
        "tenant",
    ],
)
def test_revoked_initial_dispatch_cannot_cross_changed_scope(
    discovery_scenario, catalog_wire, redis_client, fence
):
    from app.modules.tenants.models import TenantMembership

    context, run_id, bc_id = discovery_scenario
    with Session(engine) as own:
        member = own.get(TenantMembership, (context.tenant_id, context.actor_id))
        member.active = False
        run = own.get(DiscoveryRun, run_id)
        connection = own.get(TikTokConnection, run.connection_id)
        binding = own.get(
            BCConnectionBinding, (context.tenant_id, bc_id, connection.id)
        )
        if fence == "run_revision":
            run.revision += 1
        elif fence == "binding_revision":
            binding.revision += 1
        elif fence == "authorization_revision":
            connection.authorization_revision += 1
        elif fence == "disabled":
            binding.status = "DISABLED"
        elif fence == "claim":
            run.claim_id = uuid4()
            run.claimed_until = datetime.now(UTC) + timedelta(seconds=60)
        own.add_all([member, run, connection, binding])
        own.commit()
    before = len(catalog_wire.calls)
    with pytest.raises(DomainError):
        process_mcp_discovery(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=uuid4() if fence == "tenant" else context.tenant_id,
            actor_id=context.actor_id,
            payload={"run_id": str(run_id), "revision": 0},
        )
    assert len(catalog_wire.calls) == before
    with Session(engine) as own:
        run = own.get(DiscoveryRun, run_id)
        binding = own.get(
            BCConnectionBinding, (context.tenant_id, bc_id, run.connection_id)
        )
        assert run.status == "RUNNING" and run.error_code is None
        assert binding.status == ("DISABLED" if fence == "disabled" else "SYNCING")
        assert binding.last_error_code is None


@pytest.mark.parametrize(
    "invalid", ["rows", "stage", "kind", "page", "last", "scope", "digest"]
)
def test_staging_database_rejects_invalid_evidence(
    session, incomplete_mcp_run, invalid
):
    from sqlalchemy.exc import IntegrityError

    context, run = incomplete_mcp_run
    values = {
        "tenant_id": context.tenant_id,
        "connection_id": run.connection_id,
        "run_id": run.id,
        "stage": "ASSETS",
        "page": 1,
        "total_pages": 1,
        "total_number": 0,
        "last_page": True,
        "pagination_kind": "REMOTE",
        "rows": [],
        "call_evidence": {},
        "schema_digest": "a" * 64,
    }
    values.update(
        {
            "rows": {"rows": [{}] * 51},
            "stage": {"stage": "UNKNOWN"},
            "kind": {"pagination_kind": "INVENTED"},
            "page": {"page": 0},
            "last": {"last_page": False},
            "scope": {"connection_id": uuid4()},
            "digest": {"schema_digest": "bad"},
        }[invalid]
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(DiscoveryStagedPage(**values))
        session.flush()
