"""真实 Redis 公平轮次只负责排序，不重复申请 gateway 的上游额度。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis import Redis

from app.core.config import settings
from app.core.context import TenantContext
from app.integrations.tiktok.admission import quota_scope
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.jobs.admission import admission_keys
from app.modules.builds.execution_admission import admitted_build_call
from app.modules.builds.fairness import fair_keys
from tests.database import require_test_redis


@pytest.mark.parametrize("channel", ["OFFICIAL_API", "OFFICIAL_MCP"])
def test_fair_turn_uses_channel_scope_without_taking_a_second_quota(
    monkeypatch, channel
):
    url = os.environ["TEST_REDIS_URL"]
    require_test_redis(url, settings.REDIS_URL)
    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    route = FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id="synthetic",
        connection_id=uuid4(),
        channel=channel,
        authorization_revision=1,
        adapter_contract_revision="synthetic",
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_APP_ID",
        "" if channel == "OFFICIAL_MCP" else f"test-{uuid4()}",
    )
    monkeypatch.setattr(settings, "MCP_SERVICE_QUOTA_SCOPE", f"test-{uuid4()}")
    scope = quota_scope(
        channel=channel,
        app_id=settings.TIKTOK_APP_ID,
        verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
    )
    keys = admission_keys(
        scope, "build.create_campaign", context.tenant_id, "synthetic"
    )
    with Redis.from_url(url, decode_responses=True) as redis:
        try:
            with admitted_build_call(
                redis,
                context=context,
                route=route,
                task_deadline=datetime.now(UTC) + timedelta(seconds=40),
            ):
                assert all(redis.zcard(key) == 0 for key in keys)
                assert redis.get(fair_keys(scope)[1]) is None
        finally:
            redis.delete(*keys, *fair_keys(scope))
