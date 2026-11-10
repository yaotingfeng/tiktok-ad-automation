"""活跃首屏缓存的规划/构建任务；不调用广告平台。"""

import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from redis import Redis

from app.jobs.celery_app import celery_app
from app.jobs.task_routes import ensure_dispatch_queue
from app.modules.reporting.page_cache import (
    LEASE_SECONDS,
    RELEASE,
    client,
    due_key,
    lease_key,
    refresh_page,
    value,
    watch_key,
)


def plan_due(redis: Redis, publish: Callable[[str, str], None]) -> int:
    count = 0
    for raw in cast(
        list[bytes | str],
        redis.zrangebyscore(due_key(), "-inf", time.time(), start=0, num=8),
    ):
        key = value(raw)
        if key is None:
            continue
        if not redis.exists(watch_key(key)):
            redis.zrem(due_key(), key)
            continue
        token = uuid4().hex
        redis.zadd(due_key(), {key: time.time() + 5})
        if not redis.set(lease_key(key), token, nx=True, ex=LEASE_SECONDS):
            continue
        try:
            publish(key, token)
            count += 1
        except Exception:
            # 派生缓存投递失败可由下轮规划恢复，不给旧结果续期。
            redis.eval(RELEASE, 1, lease_key(key), token)
    return count


@celery_app.task(name="reporting.plan_query_pages", soft_time_limit=5, time_limit=10)  # type: ignore[untyped-decorator]
def plan_query_pages() -> int:
    ensure_dispatch_queue(plan_query_pages)

    def publish(key: str, token: str) -> None:
        celery_app.send_task(
            "reporting.refresh_query_page",
            kwargs={"key": key, "token": token},
            expires=LEASE_SECONDS,
            retry=False,
        )

    with client() as redis:
        return plan_due(redis, publish)


@celery_app.task(
    name="reporting.refresh_query_page", soft_time_limit=100, time_limit=110
)  # type: ignore[untyped-decorator]
def refresh_query_page(*, key: str, token: str) -> bool:
    ensure_dispatch_queue(refresh_query_page)
    with client() as redis:
        return refresh_page(redis, key, token)
