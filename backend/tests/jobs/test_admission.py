from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError

from app.core.errors import DomainError
from app.jobs.admission import (
    AdmissionPolicy,
    admission_keys,
    admission_policy,
    admit_call,
    release_call,
)


@pytest.fixture
def policy():
    return AdmissionPolicy(
        app_max_inflight=10,
        endpoint_max_inflight=10,
        tenant_max_inflight=10,
        advertiser_max_inflight=10,
        app_calls_per_window=10,
        endpoint_calls_per_window=10,
        window_ms=1000,
        lease_ms=60000,
    )


@pytest.fixture
def scope():
    return {
        "app_scope": f"test-{uuid4()}",
        "endpoint": "one",
        "tenant_id": uuid4(),
        "advertiser_id": "1",
    }


def call(redis_client, scope, policy, **changes):
    return admit_call(
        redis_client, **(scope | changes), lease_id=uuid4(), policy=policy
    )


def test_workers_share_application_inflight_limit(redis_client, scope, policy):
    policy.app_max_inflight = 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda i: (
                    call(
                        redis_client, scope, policy, endpoint=str(i), tenant_id=uuid4()
                    ).granted
                ),
                [1, 2],
            )
        )
    assert sorted(results) == [False, True]


def test_denied_admission_does_not_consume_other_quotas(redis_client, scope, policy):
    policy.app_max_inflight = 1
    assert call(redis_client, scope, policy).granted
    denied_scope = scope | {
        "endpoint": "two",
        "tenant_id": uuid4(),
        "advertiser_id": "2",
    }
    result = call(redis_client, denied_scope, policy)
    assert not result.granted and result.retry_after_ms > 0
    keys = admission_keys(**denied_scope)
    assert [redis_client.zcard(keys[i]) for i in (1, 3, 4, 5)] == [0, 0, 0, 0]
    assert call(redis_client, scope, policy, app_scope=f"test-{uuid4()}").granted


@pytest.mark.parametrize(
    "capacity",
    [
        "app_calls_per_window",
        "endpoint_calls_per_window",
        "app_max_inflight",
        "endpoint_max_inflight",
        "tenant_max_inflight",
        "advertiser_max_inflight",
    ],
)
def test_each_constraint_denies_atomically(redis_client, scope, policy, capacity):
    setattr(policy, capacity, 1)
    assert call(redis_client, scope, policy).granted
    assert not call(redis_client, scope, policy).granted
    assert all(redis_client.zcard(key) == 1 for key in admission_keys(**scope))


def test_endpoints_and_tenants_share_application_window(redis_client, scope, policy):
    policy.app_calls_per_window = 2
    for i in range(2):
        assert call(
            redis_client, scope, policy, endpoint=str(i), tenant_id=uuid4()
        ).granted
    assert not call(
        redis_client, scope, policy, endpoint="third", tenant_id=uuid4()
    ).granted


def test_release_does_not_refund_rate_and_duplicate_lease_is_denied(
    redis_client, scope, policy
):
    policy.app_calls_per_window = 1
    lease = uuid4()
    assert admit_call(redis_client, **scope, lease_id=lease, policy=policy).granted
    assert not admit_call(redis_client, **scope, lease_id=lease, policy=policy).granted
    release_call(redis_client, **scope, lease_id=lease)
    assert all(redis_client.zcard(key) == 0 for key in admission_keys(**scope)[2:])
    assert not call(redis_client, scope, policy).granted


def test_lost_worker_recovers_on_lease_expiry(redis_client, scope, policy):
    import time

    policy.app_max_inflight = 1
    policy.lease_ms = 50
    assert call(redis_client, scope, policy).granted
    assert not call(redis_client, scope, policy).granted
    time.sleep(0.08)
    assert call(redis_client, scope, policy).granted


def test_short_lease_preserves_existing_long_lease_ttl(redis_client, scope, policy):
    assert call(redis_client, scope, policy).granted
    policy.lease_ms = 50
    assert call(redis_client, scope, policy, endpoint="short").granted
    assert redis_client.pttl(admission_keys(**scope)[2]) > 59000


@pytest.mark.parametrize(
    "config,code",
    [
        ({}, "admission_unconfigured"),
        ({"base": {}}, "admission_policy_invalid"),
        ({"base": []}, "admission_policy_invalid"),
        ({"base": {"app_max_inflight": 0}}, "admission_policy_invalid"),
        ({"base": {}, "endpoints": []}, "admission_policy_invalid"),
        ({"base": {}, "endpoints": {"one": None}}, "admission_policy_invalid"),
    ],
)
def test_policy_configuration_fails_closed(monkeypatch, config, code):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", config)
    with pytest.raises(DomainError) as exc:
        admission_policy("one")
    assert exc.value.code == code


def test_only_endpoint_local_fields_can_be_overridden(monkeypatch, policy):
    from app.core.config import settings

    base = policy.model_dump()
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": base, "endpoints": {"one": {"lease_ms": 90000}}},
    )
    assert admission_policy("one").lease_ms == 90000
    for key in (
        "window_ms",
        "app_max_inflight",
        "tenant_max_inflight",
        "advertiser_max_inflight",
        "app_calls_per_window",
    ):
        monkeypatch.setattr(
            settings,
            "TIKTOK_CALL_POLICIES",
            {"base": base, "endpoints": {"one": {key: 1}}},
        )
        with pytest.raises(DomainError) as exc:
            admission_policy("one")
        assert exc.value.code == "admission_policy_invalid"


def test_redis_unavailable_fails_closed(scope, policy):
    class UnavailableRedis:
        def eval(self, *args):
            raise ConnectionError("offline")

    with pytest.raises(DomainError) as exc:
        admit_call(UnavailableRedis(), **scope, lease_id=uuid4(), policy=policy)
    assert exc.value.code == "admission_unavailable"
    assert exc.value.retryable


def test_all_keys_share_hash_slot(scope):
    from redis.cluster import key_slot

    assert len({key_slot(key.encode()) for key in admission_keys(**scope)}) == 1


def test_real_redis_connection_failure_is_domain_error(scope, policy):
    import socket

    from redis import Redis

    # Reserve a local TCP port without listening: connection is guaranteed refused.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        with Redis(
            host="127.0.0.1", port=port, socket_connect_timeout=0.1, socket_timeout=0.1
        ) as unavailable:
            with pytest.raises(DomainError) as exc:
                admit_call(unavailable, **scope, lease_id=uuid4(), policy=policy)
            assert exc.value.code == "admission_unavailable"


def test_empty_advertiser_group_supports_bc_calls(redis_client, scope, policy):
    assert call(redis_client, scope, policy, advertiser_id="").granted


@pytest.mark.parametrize(
    "override", [None, [], {"lease_ms": "1000"}, {"lease_ms": False}, {"lease_ms": -1}]
)
def test_invalid_override_structure_and_validation_are_domain_errors(
    monkeypatch, policy, override
):
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {"base": policy.model_dump(), "endpoints": {"one": override}},
    )
    with pytest.raises(DomainError) as exc:
        admission_policy("one")
    assert exc.value.code == "admission_policy_invalid"
