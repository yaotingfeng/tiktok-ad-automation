from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from redis.exceptions import ConnectionError

from app.core.config import TIKTOK_APP_FIELDS, settings
from app.core.errors import DomainError
from app.integrations.tiktok.admission import (
    admit_candidate_call,
    admit_tiktok_call,
    quota_scope,
)
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.sdk import AccountAdmissionDeferred, admitted_account_call
from app.jobs.admission import AdmissionPolicy, admission_keys


@pytest.fixture
def policy():
    return AdmissionPolicy(
        app_max_inflight=1,
        endpoint_max_inflight=5,
        tenant_max_inflight=5,
        advertiser_max_inflight=5,
        app_calls_per_window=100,
        endpoint_calls_per_window=100,
        window_ms=1000,
        lease_ms=60000,
    )


@pytest.fixture
def route():
    return FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc",
        connection_id=uuid4(),
        channel="OFFICIAL_MCP",
        authorization_revision=1,
        adapter_contract_revision="test-v1",
    )


def test_unknown_mcp_scope_cannot_be_split_by_api_configuration():
    assert (
        quota_scope(channel="OFFICIAL_MCP", app_id=None, verified_service_scope=None)
        == (
            quota_scope(
                channel="OFFICIAL_MCP", app_id="irrelevant", verified_service_scope=None
            )
        )
        == "official-mcp:shared-unverified"
    )


def test_api_requires_its_real_app_scope():
    assert (
        quota_scope(
            channel="OFFICIAL_API",
            app_id="verified-app",
            verified_service_scope="ignored",
        )
        == "verified-app"
    )
    with pytest.raises(DomainError) as failure:
        quota_scope(channel="OFFICIAL_API", app_id=None, verified_service_scope=None)
    assert failure.value.code == "tiktok_app_not_configured"


def test_mcp_http_call_without_api_app_keeps_api_disabled(
    redis_client, route, policy, context, monkeypatch, client_factory, mcp_wire
):
    for field in TIKTOK_APP_FIELDS:
        monkeypatch.setattr(settings, field, "")
    scope = quota_scope(
        channel=route.channel,
        app_id=settings.TIKTOK_APP_ID,
        verified_service_scope=str(uuid4()),
    )
    # 官方 SDK 会保留服务流，真实 Redis 需同时容纳业务请求与协议读取。
    concurrent_policy = policy.model_copy(update={"app_max_inflight": 10})
    owned_keys = set()

    def admit(advertiser_id, operation):
        owned_keys.update(
            admission_keys(scope, operation, route.tenant_id, advertiser_id or "")
        )
        return admit_tiktok_call(
            redis_client,
            route=route,
            advertiser_id=advertiser_id,
            operation=operation,
            scope=scope,
            policy=concurrent_policy,
        )

    try:
        with client_factory(admit=admit) as client:
            result = client.call(
                operation="builds.create_campaign",
                advertiser_id="123",
                arguments={"advertiser_id": "123"},
            )
        assert result.data == {"campaign_id": "456"}
        assert sum(c["method"] == "tools/call" for c in mcp_wire.calls) == 1
        with pytest.raises(DomainError) as failure:
            with admitted_account_call(
                redis_client,
                context=context,
                endpoint="campaign_create",
                advertiser_id="123",
                policy=policy,
            ):
                pytest.fail("API must still require its developer application")
        assert failure.value.code == "tiktok_app_not_configured"
    finally:
        if owned_keys:
            redis_client.delete(*owned_keys)


@pytest.mark.parametrize("advertiser_id", [None, "", "   "])
def test_business_call_requires_an_actual_advertiser(
    redis_client, route, policy, advertiser_id
):
    with pytest.raises(DomainError) as failure:
        with admit_tiktok_call(
            redis_client,
            route=route,
            advertiser_id=advertiser_id,
            operation="build.create_campaign",
            scope="official-mcp:shared-unverified",
            policy=policy,
        ):
            pytest.fail("No business request may use a discovery account key")
    assert failure.value.code == "account_required"


def test_two_connections_and_tenants_share_mcp_upstream(redis_client, route, policy):
    scope = quota_scope(
        channel="OFFICIAL_MCP", app_id=None, verified_service_scope=str(uuid4())
    )
    other = route.model_copy(update={"tenant_id": uuid4(), "connection_id": uuid4()})
    operation = "materials.get_videos"
    keys = [
        admission_keys(scope, operation, route.tenant_id, "a"),
        admission_keys(scope, operation, other.tenant_id, "b"),
    ]
    try:
        with admit_tiktok_call(
            redis_client,
            route=route,
            advertiser_id="a",
            operation=operation,
            scope=scope,
            policy=policy,
        ):
            with pytest.raises(AccountAdmissionDeferred):
                with admit_tiktok_call(
                    redis_client,
                    route=other,
                    advertiser_id="b",
                    operation=operation,
                    scope=scope,
                    policy=policy,
                ):
                    pytest.fail(
                        "A different connection cannot bypass the upstream bucket"
                    )
        assert all(redis_client.zcard(key) == 0 for key in keys[0][2:])
        assert redis_client.zcard(keys[0][0]) == 1
    finally:
        redis_client.delete(*(key for group in keys for key in group))


def test_candidate_attempts_share_discovery_quota(redis_client, route, policy):
    scope = quota_scope(
        channel="OFFICIAL_MCP", app_id=None, verified_service_scope=str(uuid4())
    )
    operation = "protocol.list_tools"
    keys = admission_keys(scope, operation, route.tenant_id, "")
    try:
        with admit_candidate_call(
            redis_client,
            tenant_id=route.tenant_id,
            attempt_id=uuid4(),
            scope=scope,
            operation=operation,
            policy=policy,
        ):
            with pytest.raises(AccountAdmissionDeferred):
                with admit_candidate_call(
                    redis_client,
                    tenant_id=route.tenant_id,
                    attempt_id=uuid4(),
                    scope=scope,
                    operation=operation,
                    policy=policy,
                ):
                    pytest.fail("Changing attempt cannot create a new quota domain")
    finally:
        redis_client.delete(*keys)


@pytest.mark.parametrize(
    "operation",
    [
        "materials.upload_video_url",
        "build.create_campaign",
        "scene.list_minis",
        "accounts.unreviewed_operation",
    ],
)
def test_candidate_cannot_admit_business_writes_or_unreviewed_reads(
    redis_client, route, policy, operation
):
    with pytest.raises(DomainError) as failure:
        with admit_candidate_call(
            redis_client,
            tenant_id=route.tenant_id,
            attempt_id=uuid4(),
            scope="official-mcp:shared-unverified",
            operation=operation,
            policy=policy,
        ):
            pytest.fail(
                "Candidate admission is limited to protocol and account directory"
            )
    assert failure.value.code == "mcp_candidate_operation_forbidden"


@pytest.mark.parametrize(
    "interrupt", [SystemExit, KeyboardInterrupt, SoftTimeLimitExceeded]
)
def test_interrupted_call_keeps_lease_until_expiry(
    redis_client, route, policy, interrupt
):
    scope = quota_scope(
        channel="OFFICIAL_MCP", app_id=None, verified_service_scope=str(uuid4())
    )
    operation = "build.create_campaign"
    keys = admission_keys(scope, operation, route.tenant_id, "a")
    try:
        with pytest.raises(interrupt):
            with admit_tiktok_call(
                redis_client,
                route=route,
                advertiser_id="a",
                operation=operation,
                scope=scope,
                policy=policy,
            ):
                raise interrupt()
        assert all(redis_client.zcard(key) == 1 for key in keys[2:])
        assert all(0 < redis_client.pttl(key) <= 120000 for key in keys[2:])
    finally:
        redis_client.delete(*keys)


def test_redis_release_failure_preserves_result_and_does_not_log_raw_error(
    redis_client, route, policy, monkeypatch, caplog
):
    scope = quota_scope(
        channel="OFFICIAL_MCP", app_id=None, verified_service_scope=str(uuid4())
    )
    operation = "materials.get_videos"
    keys = admission_keys(scope, operation, route.tenant_id, "a")
    original_eval = redis_client.eval

    def fail_release(script, *args):
        if "ZREM" in script and "ZADD" not in script:
            raise ConnectionError("private-token-value")
        return original_eval(script, *args)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(redis_client, "eval", fail_release)
            with admit_tiktok_call(
                redis_client,
                route=route,
                advertiser_id="a",
                operation=operation,
                scope=scope,
                policy=policy,
            ):
                receipt = "remote-id"
        assert receipt == "remote-id"
        assert "private-token-value" not in caplog.text
        assert "admission_release_failed" in caplog.text
    finally:
        redis_client.delete(*keys)
