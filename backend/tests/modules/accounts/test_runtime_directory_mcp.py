"""MCP完整再观察：真实本地MCP HTTP，保留主体/范围及当前权限边界。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.core.db import engine
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.connections import bind_candidate_bc
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from tests.modules.accounts.capabilities.test_service import run, start
from tests.modules.accounts.test_mcp_authorization import accept, issue
from tests.modules.accounts.test_mcp_directory_publish import (
    bc_page,
    enqueue_directory,
    finish,
    read_bcs,
    response,
)
from tests.modules.accounts.test_mcp_permissions import app_config as app_config
from tests.modules.accounts.test_mcp_permissions import catalog_wire as catalog_wire
from tests.modules.accounts.test_mcp_permissions import (
    committed_context as committed_context,
)
from tests.modules.accounts.test_mcp_permissions import (
    directory_context as directory_context,
)
from tests.modules.accounts.test_mcp_permissions import oauth_wire as oauth_wire
from tests.modules.accounts.test_runtime_directory import finish_runtime, runtime_step


@pytest.fixture
def expired_mcp(directory_context, oauth_wire, catalog_wire, redis_client, monkeypatch):
    from app.modules.accounts import capabilities

    monkeypatch.setattr(settings, "BC_CAPABILITY_MAX_AGE_SECONDS", 14400)
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    assert oauth_wire.calls == []
    state, candidate = issue(directory_context)
    accept(state)
    bc_id = f"bc-{directory_context.tenant_id}"
    advertiser = f"ad-{directory_context.tenant_id}"
    bc_page(catalog_wire, bcs=(bc_id,), total_number=1)
    read_bcs(directory_context, candidate, redis_client)
    with Session(engine) as session, session.begin():
        run_id = bind_candidate_bc(
            session, context=directory_context, attempt_id=candidate, bc_id=bc_id
        )
    enqueue_directory(catalog_wire, bc_id, (advertiser,))
    assert finish(directory_context, run_id, redis_client)[0] == "COMPLETE"
    with Session(engine) as session, session.begin():
        connection_id = session.get(DiscoveryRun, run_id).connection_id
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection_id
            )
        ).one()
        facts.verified_at = datetime.now(UTC) - timedelta(hours=5)
    env = {
        "context": directory_context,
        "connection_id": connection_id,
        "bc_id": bc_id,
        "request_id": uuid4(),
        "advertiser_id": advertiser,
    }
    job_id = start(env)
    run(env, redis_client, job_id)
    with Session(engine) as session:
        runtime = session.exec(
            select(DiscoveryRun).where(
                DiscoveryRun.connection_id == connection_id,
                DiscoveryRun.candidate_attempt_id.is_(None),
                DiscoveryRun.mcp_candidate_attempt_id.is_(None),
            )
        ).one()
        env.update(job_id=job_id, run_id=runtime.id)
    yield env
    from app.modules.accounts.connection_models import McpRefreshAttempt

    with Session(engine) as session, session.begin():
        session.exec(
            delete(McpRefreshAttempt).where(
                McpRefreshAttempt.tenant_id == directory_context.tenant_id
            )
        )


def test_mcp_complete_reobservation_restores_reads_but_never_writes(
    expired_mcp, catalog_wire, redis_client
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.accounts.routing import freeze_route, verify_route

    env = expired_mcp
    enqueue_directory(catalog_wire, env["bc_id"], (env["advertiser_id"],))
    finished = finish_runtime(env, redis_client, env["run_id"])
    assert finished.status == "COMPLETE", finished.error_code
    assert run(env, redis_client, env["job_id"]).status == "COMPLETE"
    with Session(engine) as session:
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        assert facts.upstream_subject == "synthetic-subject"
        assert facts.permission_summary == {
            "read_authorized": True,
            "upload_authorized": None,
            "build_authorized": None,
        }
        assert facts.verified_at > datetime.now(UTC) - timedelta(minutes=1)
        connection = session.get(TikTokConnection, env["connection_id"])
        assert (connection.credential_revision, connection.authorization_revision) == (
            1,
            1,
        )
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == connection.id
            )
        ).one()
        assert not grant.can_build and not grant.can_upload
        route = freeze_route(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=connection.id,
        )
        verify_route(
            session,
            context=env["context"],
            route=route,
            advertiser_id=env["advertiser_id"],
            capability="read",
        )


def test_changed_mcp_subject_cannot_refresh_old_facts(
    expired_mcp, catalog_wire, redis_client
):
    env = expired_mcp
    response(catalog_wire, "user_info_get", {"core_user_id": "changed-subject"})
    enqueue_directory(catalog_wire, env["bc_id"], (env["advertiser_id"],))
    finished = finish_runtime(env, redis_client, env["run_id"])
    assert (
        finished.status == "ERROR"
        and finished.error_code == "route_authorization_changed"
    )
    with Session(engine) as session:
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        assert facts.upstream_subject == "synthetic-subject"
        assert facts.verified_at < datetime.now(UTC) - timedelta(hours=4)


def test_build_permission_removed_during_mcp_initialize_blocks_business_http(
    expired_mcp, catalog_wire, redis_client, monkeypatch
):
    from app.integrations.tiktok.mcp import transport
    from app.modules.accounts.discovery_models import DiscoveryStagedPage
    from app.modules.tenants.models import TenantMembership

    env = expired_mcp
    original = transport._new_http_transport
    before = len(
        [call for call in catalog_wire.calls if call.get("method") == "tools/call"]
    )

    class RevokeAfterInitialize(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = original()

        async def handle_async_request(self, request):
            result = await self.inner.handle_async_request(request)
            if (
                request.method == "POST"
                and json.loads(request.content).get("method") == "initialize"
            ):
                with Session(engine) as session, session.begin():
                    member = session.get(
                        TenantMembership,
                        (env["context"].tenant_id, env["context"].actor_id),
                    )
                    member.role = "viewer"
            return result

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", RevokeAfterInitialize)
    result = runtime_step(env, redis_client, env["run_id"])
    assert result.status == "ERROR"
    assert (
        len([call for call in catalog_wire.calls if call.get("method") == "tools/call"])
        == before
    )
    with Session(engine) as session:
        assert (
            session.exec(
                select(DiscoveryStagedPage).where(
                    DiscoveryStagedPage.run_id == env["run_id"]
                )
            ).first()
            is None
        )


def test_expired_access_token_waits_for_normal_refresh_then_resumes_same_run(
    expired_mcp, oauth_wire, catalog_wire, redis_client
):
    from app.core.credentials import decrypt_credentials, encrypt_credentials
    from app.integrations.tiktok.mcp_auth.refresh import process_mcp_refresh
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.accounts.connection_models import McpRefreshAttempt

    env = expired_mcp
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, env["connection_id"])
        material = decrypt_credentials(
            tenant_id=connection.tenant_id, ciphertext=connection.credential_ciphertext
        )
        material["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=connection.tenant_id, value=material
        )
        old_fact_time = (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == connection.id
                )
            )
            .one()
            .verified_at
        )
    before_business, before_oauth = len(catalog_wire.calls), len(oauth_wire.calls)
    waiting = runtime_step(env, redis_client, env["run_id"])
    assert (
        waiting.status == "ADMISSION_WAIT"
        and waiting.error_code == "mcp_refresh_pending"
    )
    assert (
        len(catalog_wire.calls) == before_business
        and len(oauth_wire.calls) == before_oauth
    )
    with Session(engine) as session:
        attempt = session.exec(
            select(McpRefreshAttempt).where(
                McpRefreshAttempt.connection_id == env["connection_id"]
            )
        ).one()
        attempt_id = attempt.id
        assert attempt.status == "PENDING"
        assert session.get(CapabilityJob, env["job_id"]).status == "PENDING"
        assert session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == env["context"].tenant_id,
                PendingDispatch.task_name == "accounts.refresh_mcp",
            )
        ).one().payload == {"attempt_id": str(attempt_id)}
    oauth_wire.data["access_token"] = "synthetic-rotated-runtime-access"
    assert (
        process_mcp_refresh(
            database_engine=engine,
            redis_client=redis_client,
            context=env["context"],
            attempt_id=attempt_id,
        )
        == "PUBLISHED"
    )
    assert len(oauth_wire.calls) == before_oauth + 1
    assert oauth_wire.calls[-1]["grant_type"] == ["refresh_token"]
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, env["connection_id"])
        assert (connection.credential_revision, connection.authorization_revision) == (
            2,
            1,
        )
        assert (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == connection.id
                )
            )
            .one()
            .verified_at
            == old_fact_time
        )
        row = session.get(DiscoveryRun, env["run_id"])
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    enqueue_directory(catalog_wire, env["bc_id"], (env["advertiser_id"],))
    assert finish_runtime(env, redis_client, env["run_id"]).status == "COMPLETE"
    completed = run(env, redis_client, env["job_id"])
    assert completed.id == env["job_id"] and completed.status == "COMPLETE"


def test_unknown_refresh_outcome_blocks_runtime_without_business_http(
    expired_mcp, catalog_wire, redis_client
):
    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.accounts.connection_models import McpRefreshAttempt

    env = expired_mcp
    with Session(engine) as session, session.begin():
        session.add(
            McpRefreshAttempt(
                tenant_id=env["context"].tenant_id,
                connection_id=env["connection_id"],
                base_credential_revision=1,
                base_authorization_revision=1,
                status="OUTCOME_UNKNOWN",
                request_armed_at=datetime.now(UTC),
            )
        )
    before = len(catalog_wire.calls)
    result = runtime_step(env, redis_client, env["run_id"])
    assert result.status == "ERROR" and result.error_code == "mcp_refresh_unknown"
    assert len(catalog_wire.calls) == before
    with Session(engine) as session:
        assert session.get(CapabilityJob, env["job_id"]).status == "BLOCKED"
