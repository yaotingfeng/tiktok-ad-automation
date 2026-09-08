from uuid import uuid4

import pytest

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.admission import admission_keys


def policy(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", f"build-test-{uuid4()}")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "fixture")
    monkeypatch.setattr(
        settings, "TIKTOK_REDIRECT_URI", "https://example.test/callback"
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "base": {
                "app_max_inflight": 3,
                "endpoint_max_inflight": 1,
                "tenant_max_inflight": 2,
                "advertiser_max_inflight": 1,
                "app_calls_per_window": 20,
                "endpoint_calls_per_window": 20,
                "window_ms": 10000,
                "lease_ms": 60000,
            }
        },
    )


def test_write_quota_covers_call_but_not_fair_turn_and_finally_releases(
    redis_client, monkeypatch
):
    from app.modules.builds.execution_admission import admitted_build_call
    from app.modules.builds.fairness import fair_keys

    policy(monkeypatch)
    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    endpoint = "/fixture/build/create/"
    keys = admission_keys(
        settings.TIKTOK_APP_ID, endpoint, context.tenant_id, "account-1"
    )
    with pytest.raises(RuntimeError):
        with admitted_build_call(
            redis_client, context=context, endpoint=endpoint, advertiser_id="account-1"
        ):
            assert all(redis_client.zcard(key) == 1 for key in keys[2:])
            assert redis_client.get(fair_keys(settings.TIKTOK_APP_ID)[1]) is None
            raise RuntimeError("synthetic call failure")
    assert all(redis_client.zcard(key) == 0 for key in keys[2:])
    assert all(redis_client.zcard(key) == 1 for key in keys[:2])


def test_denied_account_releases_fair_turn_for_other_tenant(redis_client, monkeypatch):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.modules.builds.execution_admission import admitted_build_call

    policy(monkeypatch)
    a = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    b = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    with admitted_build_call(
        redis_client, context=a, endpoint="/first/", advertiser_id="busy"
    ):
        with pytest.raises(AccountAdmissionDeferred):
            with admitted_build_call(
                redis_client, context=a, endpoint="/second/", advertiser_id="busy"
            ):
                raise AssertionError("denied call executed")
        with admitted_build_call(
            redis_client, context=b, endpoint="/second/", advertiser_id="free"
        ):
            pass


def test_lease_must_outlive_whole_prefork_deadline(redis_client, monkeypatch):
    from app.modules.builds.execution_admission import admitted_build_call

    policy(monkeypatch)
    settings.TIKTOK_CALL_POLICIES["base"]["lease_ms"] = 45000
    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    with pytest.raises(DomainError) as caught:
        with admitted_build_call(
            redis_client, context=context, endpoint="/create/", advertiser_id="a"
        ):
            raise AssertionError("unsafe call executed")
    assert caught.value.code == "admission_policy_invalid"
