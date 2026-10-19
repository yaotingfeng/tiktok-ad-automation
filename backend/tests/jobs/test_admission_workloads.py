"""真实 Redis 验证后台持续请求不能占满实时额度，实时也不能饿死后台。"""

from uuid import uuid4

import pytest

from app.jobs.admission import AdmissionPolicy, admit_call, release_call


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


def take(redis, scope, policy, workload):
    lease = uuid4()
    return lease, admit_call(
        redis, **scope, lease_id=lease, policy=policy, workload=workload
    )


@pytest.mark.parametrize("workload", ["live", "background"])
def test_recent_other_demand_does_not_reserve_unused_capacity(
    redis_client, scope, policy, workload
):
    policy.app_max_inflight = 4
    policy.app_calls_per_window = policy.endpoint_calls_per_window = 100
    lease, result = take(
        redis_client, scope, policy, "background" if workload == "live" else "live"
    )
    assert result.granted
    release_call(redis_client, **scope, lease_id=lease)
    for _ in range(4):
        assert take(redis_client, scope, policy, workload)[1].granted
    assert not take(redis_client, scope, policy, workload)[1].granted


def test_idle_share_is_borrowed_without_splitting_global_quota(
    redis_client, scope, policy
):
    policy.app_max_inflight = 4
    for _ in range(4):
        assert take(redis_client, scope, policy, "background")[1].granted
    assert not take(redis_client, scope, policy, "live")[1].granted


def test_endpoint_shares_preserve_app_scope_across_connections(
    redis_client, scope, policy
):
    policy.app_calls_per_window = 4
    for workload in ("live", "background", "live", "live"):
        changed = scope | {"endpoint": str(uuid4()), "advertiser_id": str(uuid4())}
        lease, result = take(redis_client, changed, policy, workload)
        assert result.granted
        release_call(redis_client, **changed, lease_id=lease)
    assert not take(redis_client, scope, policy, "background")[1].granted


def test_single_slot_constraints_cannot_deadlock_across_endpoints(
    redis_client, scope, policy
):
    policy.app_calls_per_window = 100
    policy.endpoint_calls_per_window = 100
    policy.endpoint_max_inflight = 1
    # 两个端点上一次分别由不同服务使用；不用轮次预留空槽。
    first = scope | {"endpoint": "first"}
    second = scope | {"endpoint": "second"}
    lease, result = take(redis_client, first, policy, "live")
    assert result.granted
    release_call(redis_client, **first, lease_id=lease)
    lease, result = take(redis_client, second, policy, "background")
    assert result.granted
    release_call(redis_client, **second, lease_id=lease)
    for _ in range(6):
        results = []
        for workload in ("live", "background"):
            for target in (first, second):
                lease, result = take(redis_client, target, policy, workload)
                results.append(result.granted)
                if result.granted:
                    release_call(redis_client, **target, lease_id=lease)
        assert any(results), "不能因多个单槽轮次互相矛盾形成死锁"


def test_continuous_live_and_background_load_both_make_progress(
    redis_client, scope, policy
):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic, sleep

    policy.app_max_inflight = 4
    policy.endpoint_max_inflight = 3
    policy.app_calls_per_window = 20
    policy.endpoint_calls_per_window = 20
    policy.window_ms = 100
    deadline = monotonic() + 2

    def produce(workload):
        granted = 0
        while monotonic() < deadline:
            lease, result = take(redis_client, scope, policy, workload)
            if result.granted:
                granted += 1
                sleep(0.002)
                release_call(redis_client, **scope, lease_id=lease)
            else:
                sleep(0.002)
        return granted

    with ThreadPoolExecutor(max_workers=2) as pool:
        live, background = list(pool.map(produce, ["live", "background"]))
    assert live >= 20 and background >= 10
    assert live + background <= 440  # 原共享20次/100ms的上限，加两个边界窗口。


def test_independent_provider_endpoints_do_not_share_rate_budget(
    redis_client, scope, policy
):
    policy.app_calls_per_window = 2
    policy.endpoint_calls_per_window = 2
    for endpoint in ("report", "overview"):
        for _ in range(2):
            lease = uuid4()
            result = admit_call(
                redis_client,
                **(scope | {"endpoint": endpoint}),
                lease_id=lease,
                policy=policy,
                rate_group=f"GET /{endpoint}/",
            )
            assert result.granted
            release_call(
                redis_client, **(scope | {"endpoint": endpoint}), lease_id=lease
            )
        result = admit_call(
            redis_client,
            **(scope | {"endpoint": endpoint}),
            lease_id=uuid4(),
            policy=policy,
            rate_group=f"GET /{endpoint}/",
        )
        assert not result.granted


def test_local_denial_reports_the_actual_resource(redis_client, scope, policy):
    policy.endpoint_max_inflight = 1
    assert take(redis_client, scope, policy, "live")[1].granted
    result = take(redis_client, scope, policy, "background")[1]
    assert not result.granted and result.reason == "endpoint_inflight"


def test_failed_telemetry_preserves_provider_backoff(redis_client, scope, policy):
    from app.core.errors import DomainError
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope

    class DeadlineRedis:
        def eval(self, *args):
            return redis_client.eval(*args)

        def hincrby(self, *args):
            raise DomainError("read_deadline_exceeded", "deadline")

    original = AccountAdmissionDeferred(86400000, source="provider", reason="qpd")
    with pytest.raises(AccountAdmissionDeferred) as caught:
        with admitted_scope(
            DeadlineRedis(),
            **scope,
            policy=policy,
            denied_error=AccountAdmissionDeferred,
        ):
            raise original
    assert caught.value is original
