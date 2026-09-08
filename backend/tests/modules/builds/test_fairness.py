from uuid import UUID, uuid4

from app.modules.builds.fairness import fair_keys, finish_fair_turn, take_fair_turn


def test_waiting_tenant_gets_next_turn(redis_client):
    a, b, owner = UUID(int=1), UUID(int=2), uuid4()
    scope = f"fair-test-{uuid4().hex}"
    args = {"app_scope": scope, "wait_ms": 30000, "turn_ms": 3000}
    try:
        assert take_fair_turn(redis_client, tenant_id=a, owner=owner, **args)
        assert not take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
        finish_fair_turn(
            redis_client, app_scope=scope, tenant_id=a, owner=owner, served=True
        )
        assert not take_fair_turn(redis_client, tenant_id=a, owner=uuid4(), **args)
        assert take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
    finally:
        redis_client.delete(*fair_keys(scope))


def test_denial_delays_only_current_tenant_and_stale_owner_cannot_release(redis_client):
    a, b, owner = UUID(int=3), UUID(int=4), uuid4()
    scope = f"fair-denial-{uuid4().hex}"
    args = {"app_scope": scope, "wait_ms": 30000, "turn_ms": 3000}
    try:
        assert take_fair_turn(redis_client, tenant_id=a, owner=owner, **args)
        finish_fair_turn(
            redis_client, app_scope=scope, tenant_id=a, owner=uuid4(), served=True
        )
        assert not take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
        finish_fair_turn(
            redis_client,
            app_scope=scope,
            tenant_id=a,
            owner=owner,
            served=False,
            retry_after_ms=5000,
        )
        assert not take_fair_turn(redis_client, tenant_id=a, owner=uuid4(), **args)
        assert take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
    finally:
        redis_client.delete(*fair_keys(scope))


def test_same_tenant_concurrency_gets_one_short_turn(redis_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    scope = f"fair-concurrent-{uuid4().hex}"
    tenant = uuid4()
    barrier = Barrier(2)

    def take(_):
        barrier.wait(timeout=5)
        return take_fair_turn(
            redis_client,
            app_scope=scope,
            tenant_id=tenant,
            owner=uuid4(),
            wait_ms=30000,
            turn_ms=3000,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(take, range(2))) == [False, True]
    finally:
        redis_client.delete(*fair_keys(scope))


def test_bounded_cleanup_cannot_leave_delayed_tenant_at_ready_head(redis_client):
    scope = f"fair-bounded-{uuid4().hex}"
    keys = fair_keys(scope)
    clock = redis_client.time()
    now = clock[0] * 1000 + clock[1] // 1000
    try:
        # Thousands of deferred entries never require a full-list Lua scan.
        redis_client.zadd(
            keys[4], {str(UUID(int=i)): now + 60000 for i in range(1, 2001)}
        )
        redis_client.zadd(keys[3], {str(UUID(int=i)): now for i in range(1, 2001)})
        tenant, owner = uuid4(), uuid4()
        assert take_fair_turn(
            redis_client,
            app_scope=scope,
            tenant_id=tenant,
            owner=owner,
            wait_ms=30000,
            turn_ms=3000,
        )
        assert redis_client.zcard(keys[4]) == 2000
        finish_fair_turn(
            redis_client, app_scope=scope, tenant_id=tenant, owner=owner, served=True
        )
        assert redis_client.zcard(keys[0]) == 0
    finally:
        redis_client.delete(*keys)


def test_expired_waiter_is_removed_and_owner_release_is_fenced(redis_client):
    scope = f"fair-expired-{uuid4().hex}"
    keys = fair_keys(scope)
    tenant, owner = uuid4(), uuid4()
    try:
        assert take_fair_turn(
            redis_client,
            app_scope=scope,
            tenant_id=tenant,
            owner=owner,
            wait_ms=30000,
            turn_ms=3000,
        )
        # Simulate a crashed owner's naturally expired turn and heartbeat.
        redis_client.delete(keys[1])
        redis_client.zadd(keys[3], {str(tenant): 0})
        next_tenant, next_owner = uuid4(), uuid4()
        assert take_fair_turn(
            redis_client,
            app_scope=scope,
            tenant_id=next_tenant,
            owner=next_owner,
            wait_ms=30000,
            turn_ms=3000,
        )
        finish_fair_turn(
            redis_client, app_scope=scope, tenant_id=tenant, owner=owner, served=True
        )
        assert redis_client.get(keys[1]) == f"{next_tenant}:{next_owner}"
        assert redis_client.zscore(keys[0], str(tenant)) is None
    finally:
        redis_client.delete(*keys)


def test_redis_failure_is_a_safe_denial():
    import pytest
    from redis.exceptions import ConnectionError

    from app.core.errors import DomainError

    class Unavailable:
        def eval(self, *_args):
            raise ConnectionError("private transport details")

    with pytest.raises(DomainError) as error:
        take_fair_turn(
            Unavailable(),
            app_scope="test",
            tenant_id=uuid4(),
            owner=uuid4(),
            wait_ms=30000,
            turn_ms=3000,
        )
    assert error.value.code == "admission_unavailable"
    assert "private" not in str(error.value)
