from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.jobs.admission import admission_keys, admit_call
from app.modules.sync_control.occupancy import occupancy_keys, reserve_dispatch
from tests.modules.sync_control.test_endpoint_readiness import _policy, _scope


def test_successful_nested_reservation_is_released_if_outer_transaction_fails(
    redis_client,
):
    from app.modules.sync_control.occupancy import reservation_transaction

    scope, token = _scope(), uuid4()
    with pytest.raises(RuntimeError, match="outer failure"):
        with reservation_transaction(redis_client):
            with reservation_transaction(redis_client):
                assert (
                    reserve_dispatch(
                        redis_client, reservation_id=token, policy=_policy(), **scope
                    ).capacity_hint
                    > 0
                )
            raise RuntimeError("outer failure")
    assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))


def test_two_controllers_cannot_reserve_same_free_slot(redis_client):
    scope, policy = _scope(), _policy(endpoint_calls_per_window=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: reserve_dispatch(
                    redis_client, reservation_id=uuid4(), policy=policy, **scope
                ),
                range(2),
            )
        )
    assert sorted(result.capacity_hint for result in results) == [0, 1]
    assert redis_client.zcard(admission_keys(**scope)[1]) == 0
    assert redis_client.zcard(occupancy_keys(**scope)[1]) == 1


def test_send_handoff_removes_queued_occupancy_and_consumes_once(redis_client):
    scope, policy, token = _scope(), _policy(), str(uuid4())
    assert (
        reserve_dispatch(
            redis_client, reservation_id=token, policy=policy, **scope
        ).capacity_hint
        == 10
    )
    assert admit_call(
        redis_client,
        policy=policy,
        lease_id=uuid4(),
        dispatch_reservation_id=token,
        **scope,
    ).granted
    assert all(redis_client.zcard(key) == 0 for key in occupancy_keys(**scope))
    assert all(redis_client.zcard(key) == 1 for key in admission_keys(**scope))


def test_queued_endpoint_does_not_spend_independent_endpoint_budget(redis_client):
    scope, policy = _scope(), _policy(endpoint_calls_per_window=1)
    reserve_dispatch(redis_client, reservation_id=uuid4(), policy=policy, **scope)
    other = scope | {"endpoint": "independent", "rate_group": "independent"}
    assert (
        reserve_dispatch(
            redis_client, reservation_id=uuid4(), policy=policy, **other
        ).capacity_hint
        == 1
    )


def test_running_quantum_uses_real_quota_instead_of_requeueing_behind_promises(
    redis_client,
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope
    from app.modules.sync_control.occupancy import dispatch_context

    scope = _scope()
    policy = _policy(endpoint_calls_per_window=3, window_ms=60000)
    tokens = [uuid4() for _ in range(3)]
    for token in tokens:
        assert (
            reserve_dispatch(
                redis_client, reservation_id=token, policy=policy, **scope
            ).capacity_hint
            > 0
        )
    with dispatch_context(redis_client, reservation_id=tokens[0], scope=scope):
        for _ in range(3):
            with admitted_scope(
                redis_client,
                policy=policy,
                denied_error=AccountAdmissionDeferred,
                **scope,
            ):
                pass
        # 后续页仍必须经过原Lua；不能把不再排队解释为跳过真实次数上限。
        with pytest.raises(AccountAdmissionDeferred):
            with admitted_scope(
                redis_client,
                policy=policy,
                denied_error=AccountAdmissionDeferred,
                **scope,
            ):
                pytest.fail("real frequency limit was bypassed")
    keys = admission_keys(**scope)
    assert redis_client.zcard(keys[1]) == 3
    assert redis_client.zcard(keys[3]) == 0
    # 其他消息的排队凭据不被运行中的续页删除。
    assert redis_client.zcard(occupancy_keys(**scope)[1]) == 2


@pytest.mark.parametrize("blocker", ["inflight", "cooldown"])
def test_running_quantum_still_obeys_actual_inflight_and_provider_cooldown(
    redis_client, blocker
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope, release_call
    from app.modules.sync_control.occupancy import dispatch_context

    scope, policy, token = _scope(), _policy(endpoint_max_inflight=1), uuid4()
    assert (
        reserve_dispatch(
            redis_client, reservation_id=token, policy=policy, **scope
        ).capacity_hint
        > 0
    )
    held = uuid4()
    with dispatch_context(redis_client, reservation_id=token, scope=scope):
        with admitted_scope(
            redis_client, policy=policy, denied_error=AccountAdmissionDeferred, **scope
        ):
            pass
        if blocker == "inflight":
            assert admit_call(
                redis_client, lease_id=held, policy=policy, **scope
            ).granted
        else:
            seconds, micros = redis_client.time()
            redis_client.set(
                admission_keys(**scope)[1] + ":cooldown",
                seconds * 1000 + micros // 1000 + 60000,
                px=60000,
            )
        try:
            with pytest.raises(AccountAdmissionDeferred):
                with admitted_scope(
                    redis_client,
                    policy=policy,
                    denied_error=AccountAdmissionDeferred,
                    **scope,
                ):
                    pytest.fail("actual admission protection was bypassed")
        finally:
            if blocker == "inflight":
                release_call(
                    redis_client,
                    lease_id=held,
                    **{k: v for k, v in scope.items() if k != "rate_group"},
                )


def test_cross_endpoint_continuation_keeps_queued_ticket_and_uses_its_actual_limit(
    redis_client,
):
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred
    from app.jobs.admission import admitted_scope
    from app.modules.sync_control.occupancy import dispatch_context

    scope, policy, token = _scope(), _policy(endpoint_calls_per_window=1), uuid4()
    other = scope | {"endpoint": "second-endpoint", "rate_group": "second-endpoint"}
    other_token = uuid4()
    for current, ticket in [(scope, token), (other, other_token)]:
        assert (
            reserve_dispatch(
                redis_client, reservation_id=ticket, policy=policy, **current
            ).capacity_hint
            > 0
        )
    with dispatch_context(redis_client, reservation_id=token, scope=scope):
        for current in [scope, other]:
            with admitted_scope(
                redis_client,
                policy=policy,
                denied_error=AccountAdmissionDeferred,
                **current,
            ):
                pass
        with pytest.raises(AccountAdmissionDeferred):
            with admitted_scope(
                redis_client,
                policy=policy,
                denied_error=AccountAdmissionDeferred,
                **other,
            ):
                pytest.fail("new endpoint frequency limit was bypassed")
    assert redis_client.zscore(occupancy_keys(**other)[1], str(other_token)) is not None
    assert redis_client.zcard(admission_keys(**other)[1]) == 1
