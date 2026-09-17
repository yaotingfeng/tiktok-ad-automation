"""排队消息的短期索引；实时确认存在才抑制补投，不充当业务完成证据。"""

import base64
import json
from hashlib import sha256
from time import monotonic
from typing import Any, cast

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.jobs.celery_app import celery_app

_QUEUES = ("control", "builds", "resources", "resource-results")
_CACHE_SECONDS = 5.0
_SCAN_SECONDS = 1.0
_MAX_MESSAGES = 40_000


def _identity(name: str, task_id: str, kwargs: dict[str, Any]) -> tuple[str, str]:
    data = json.dumps([name, kwargs], sort_keys=True, separators=(",", ":"))
    return task_id, sha256(data.encode()).hexdigest()


def _message_identity(raw: bytes | str) -> tuple[str, str] | None:
    try:
        if len(raw) > 65_536:
            return None
        message = json.loads(raw)
        headers, properties = message["headers"], message["properties"]
        if not isinstance(headers, dict) or not isinstance(properties, dict):
            return None
        name, task_id = headers["task"], headers["id"]
        if not isinstance(name, str) or not isinstance(task_id, str):
            return None
        body = message["body"]
        if properties.get("body_encoding") == "base64":
            body = base64.b64decode(body, validate=True)
        decoded = json.loads(body)
        if not isinstance(decoded, list) or len(decoded) != 3:
            return None
        args, kwargs, _embedded = decoded
        if args != [] or not isinstance(kwargs, dict):
            return None
        return _identity(name, task_id, kwargs)
    except KeyError, TypeError, ValueError, RecursionError:
        return None


class QueuedDispatches:
    def __init__(self, redis: Redis, keys: dict[str, str]):
        self.redis = redis
        self.keys = keys
        self.entries: dict[tuple[str, str, str], tuple[str, bytes | str]] = {}

    def _index(self, key: str, messages: list[Any]) -> None:
        for raw in messages:
            identity = _message_identity(raw)
            if identity is not None:
                self.entries[(self.keys[key], *identity)] = (key, raw)

    def scan(self) -> None:
        deadline, count = monotonic() + _SCAN_SECONDS, 0
        try:
            for key in self.keys:
                for start in range(0, _MAX_MESSAGES, 256):
                    if count >= _MAX_MESSAGES or monotonic() >= deadline:
                        return
                    rows = cast(list[bytes], self.redis.lrange(key, start, start + 255))
                    self._index(key, rows)
                    count += len(rows)
                    if len(rows) < 256:
                        break
        except RedisError:
            # 不完整索引最多漏抑制重复，不得阻止持久任务重新投递。
            return

    def contains(
        self, *, queue: str, name: str, task_id: str, kwargs: dict[str, Any]
    ) -> bool:
        identity = _identity(name, task_id, kwargs)
        # 发布换队列期间，旧 resources 中的同一核实消息仍是有效投递。
        eligible = {queue, "resources"} if queue == "resource-results" else {queue}
        try:
            for actual in eligible:
                found = self.entries.get((actual, *identity))
                if found is None:
                    # 新消息在 LPUSH 端，补齐缓存后的少量新增，无须再次全表扫描。
                    for key, logical in self.keys.items():
                        if logical == actual:
                            self._index(
                                key, cast(list[bytes], self.redis.lrange(key, 0, 15))
                            )
                    found = self.entries.get((actual, *identity))
                if found is not None:
                    key, raw = found
                    # 索引可能过期。缓存命中不能吞掉 broker 中已经丢失的消息。
                    if self.redis.execute_command("LPOS", key, raw) is not None:
                        return True
                    self.entries.pop((actual, *identity), None)
        except RedisError:
            return False
        return False


_cache: tuple[tuple[Any, ...], float, QueuedDispatches] | None = None


def queued_dispatches() -> QueuedDispatches:
    global _cache
    options = celery_app.conf.broker_transport_options or {}
    prefix = options.get("global_keyprefix", "")
    steps = tuple(options.get("priority_steps", (0, 3, 6, 9)))
    cache_key = (settings.REDIS_URL, prefix, steps)
    if _cache is not None and _cache[0] == cache_key and monotonic() < _cache[1]:
        return _cache[2]
    redis = Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry_on_timeout=False,
        max_connections=4,
    )
    keys = {
        prefix + queue + (f"\x06\x16{priority}" if priority else ""): queue
        for queue in _QUEUES
        for priority in steps
    }
    snapshot = QueuedDispatches(redis, keys)
    snapshot.scan()
    _cache = (cache_key, monotonic() + _CACHE_SECONDS, snapshot)
    return snapshot
