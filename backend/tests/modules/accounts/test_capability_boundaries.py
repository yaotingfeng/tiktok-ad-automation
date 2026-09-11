"""能力重检请求授权、刷新终态与目录元数据隔离；真实 PG 和 MCP HTTP。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
from mcp.types import CallToolResult
from sqlmodel import Session, delete

from app.modules.accounts import capabilities
from app.modules.accounts.capability_models import (
    CapabilityAsset,
    CapabilityJob,
    CapabilityPage,
    CapabilityRequest,
)
from app.modules.accounts.connection_models import McpRefreshAttempt
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    DiscoveryRun,
    TikTokConnection,
)
from app.modules.tenants.models import TenantMembership
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire


@pytest.fixture
def capability_case(database_engine, gateway_case, monkeypatch):
    context, route, advertiser = gateway_case
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    with Session(database_engine) as db, db.begin():
        job_id = capabilities.start_capability_refresh(
            db,
            context=context,
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            request_id=uuid4(),
        )
    try:
        yield context, route, advertiser, job_id
    finally:
        with Session(database_engine) as db, db.begin():
            for model in (
                CapabilityAsset,
                CapabilityPage,
                CapabilityRequest,
                CapabilityJob,
            ):
                db.exec(delete(model).where(model.tenant_id == context.tenant_id))


def run_capability(database_engine, redis_client, case):
    context, _, _, job_id = case
    with Session(database_engine) as db, db.begin():
        job = db.get(CapabilityJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = job.revision
    capabilities.process_capability(
        database_engine=database_engine,
        redis_client=redis_client,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"job_id": str(job_id), "revision": revision},
    )
    with Session(database_engine) as db:
        return db.get(CapabilityJob, job_id)


@pytest.mark.parametrize("change", ["viewer", "claim"])
def test_capability_rechecks_current_actor_and_claim_before_business_http(
    database_engine,
    redis_client,
    capability_case,
    gateway_wire,
    monkeypatch,
    change,
):
    from app.integrations.tiktok.mcp import transport

    context, _, advertiser, job_id = capability_case
    gateway_wire["wire"].enqueue_result(
        "bc_asset_get",
        CallToolResult(
            content=[],
            structuredContent={
                "code": 0,
                "data": {
                    "list": [
                        {
                            "asset_id": advertiser,
                            "asset_type": "ADVERTISER",
                            "advertiser_role": "ADMIN",
                        }
                    ],
                    "page_info": {
                        "page": 1,
                        "page_size": 50,
                        "total_page": 1,
                        "total_number": 1,
                    },
                },
            },
        ),
    )
    original = transport._new_http_transport

    class ChangeAfterInitialize(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = original()

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if (
                request.method == "POST"
                and json.loads(request.content).get("method") == "initialize"
            ):
                with Session(database_engine) as db, db.begin():
                    if change == "viewer":
                        db.get(
                            TenantMembership, (context.tenant_id, context.actor_id)
                        ).role = "viewer"
                    else:
                        db.get(CapabilityJob, job_id).claim_token = uuid4()
            return response

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", ChangeAfterInitialize)
    run_capability(database_engine, redis_client, capability_case)
    calls = [r for r in gateway_wire["wire"].calls if r.get("method") == "tools/call"]
    assert calls == [], "当下build权限或claim失效后仍发送角色业务读取"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("OUTCOME_UNKNOWN", "mcp_refresh_unknown"),
        ("NO_REFRESH_TOKEN", "mcp_refresh_reauth_required"),
        ("REFRESH_PENDING", "mcp_refresh_pending"),
    ],
)
def test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever(
    database_engine,
    redis_client,
    capability_case,
    gateway_wire,
    state,
    expected,
):
    context, route, _, _ = capability_case
    with Session(database_engine) as db, db.begin():
        if state == "OUTCOME_UNKNOWN":
            db.add(
                McpRefreshAttempt(
                    tenant_id=context.tenant_id,
                    connection_id=route.connection_id,
                    base_credential_revision=1,
                    base_authorization_revision=1,
                    status=state,
                    request_armed_at=datetime.now(UTC),
                )
            )
        else:
            from app.core.credentials import decrypt_credentials, encrypt_credentials

            connection = db.get(TikTokConnection, route.connection_id)
            material = decrypt_credentials(
                tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
            )
            material["expires_at"] = (
                datetime.now(UTC) - timedelta(minutes=1)
            ).isoformat()
            if state == "NO_REFRESH_TOKEN":
                material.pop("refresh_token", None)
            else:
                material["refresh_token"] = "synthetic-refresh-token"
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=context.tenant_id, value=material
            )
    job = run_capability(database_engine, redis_client, capability_case)
    assert gateway_wire["wire"].calls == []
    assert (job.status, job.error_code) == (
        "PENDING" if state == "REFRESH_PENDING" else "BLOCKED",
        expected,
    )


@pytest.mark.parametrize("has_details", [False, True])
def test_only_observed_details_update_shared_metadata(
    database_engine, gateway_case, has_details
):
    from app.modules.accounts.directory_merge import merge_directory_bc

    context, route, advertiser = gateway_case
    # 整个探针事务最终回滚；另一连接只看见资产、没有授权详情是合法空交集。
    with Session(database_engine) as db:
        old = db.get(AdvertiserAccount, (context.tenant_id, advertiser))
        original = old.currency, old.timezone, old.remote_status
        other = TikTokConnection(tenant_id=context.tenant_id, kind="OFFICIAL_API")
        db.add(other)
        db.flush()
        run = DiscoveryRun(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            connection_id=other.id,
        )
        db.add(run)
        db.flush()
        details = (
            [
                {
                    "advertiser_id": advertiser,
                    "name": "Actually observed",
                    "currency": "GBP",
                    "timezone": "Europe/London",
                    "remote_status": "STATUS_ENABLE",
                }
            ]
            if has_details
            else []
        )
        for stage, rows, scope, kind in (
            (
                "AUTHORIZED",
                [{"advertiser_id": advertiser}] if has_details else [],
                "",
                "FULL_RESPONSE",
            ),
            (
                "ASSETS",
                [
                    {"advertiser_id": advertiser, "name": "Synthetic"},
                    {"advertiser_id": "new-unobserved-account", "name": "New asset"},
                ],
                route.bc_id,
                "REMOTE",
            ),
            ("DETAILS", details, route.bc_id, "EXPLICIT_IDS"),
        ):
            db.add(
                DiscoveryStagedPage(
                    tenant_id=context.tenant_id,
                    connection_id=other.id,
                    run_id=run.id,
                    bc_id=scope,
                    stage=stage,
                    page=1,
                    total_pages=1,
                    total_number=len(rows),
                    last_page=True,
                    pagination_kind=kind,
                    rows=rows,
                    call_evidence={},
                    schema_digest="a" * 64,
                )
            )
        db.flush()
        merge_directory_bc(db, run=run, bc_id=route.bc_id, bc_name="Synthetic")
        db.refresh(old)
        assert (old.currency, old.timezone, old.remote_status) == (
            ("GBP", "Europe/London", "STATUS_ENABLE") if has_details else original
        )
        new = db.get(AdvertiserAccount, (context.tenant_id, "new-unobserved-account"))
        assert (new.currency, new.timezone, new.remote_status) == ("", "", "UNKNOWN")
        grant = db.get(
            BCAccountAccess, (context.tenant_id, route.bc_id, advertiser, other.id)
        )
        assert grant.authorized is has_details and grant.active is has_details
        assert not grant.can_build and not grant.can_upload
