"""停写发布窗口内迁移旧结果消息；保留原字节、任务 ID 和重复副本。"""

import json
from collections import Counter
from hashlib import sha256
from typing import cast

from redis import Redis

from app.jobs.queued_dispatches import _message_identity
from app.jobs.tasks import _RESOURCE_RESULTS

_MOVE = """
local target_type = redis.call('TYPE', KEYS[2]).ok
if target_type ~= 'none' and target_type ~= 'list' then
  return redis.error_reply('destination_is_not_a_list')
end
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then redis.call('LPUSH', KEYS[2], ARGV[1]) end
return removed
"""


def move_result_messages(
    redis: Redis, *, prefix: str = "", priorities: tuple[int, ...] = (0, 3, 6, 9)
) -> dict[str, int]:
    """调用方必须先停止 API、Beat、全部消费者并验证可恢复备份。"""
    pairs = [
        (
            prefix + "resources" + (f"\x06\x16{priority}" if priority else ""),
            prefix + "resource-results" + (f"\x06\x16{priority}" if priority else ""),
        )
        for priority in priorities
    ]
    before: Counter = Counter()
    after: Counter = Counter()
    moved = 0
    for source, target in pairs:
        rows = cast(list[bytes | str], redis.lrange(source, 0, -1))
        existing = cast(list[bytes | str], redis.lrange(target, 0, -1))
        before.update(_digest(raw) for raw in [*rows, *existing])
        # 从原 FIFO 的最老消息开始 LPUSH，保持被移动成员的先后顺序。
        for raw in reversed(rows):
            if (
                _message_identity(raw) is not None
                and json.loads(raw)["headers"]["task"] in _RESOURCE_RESULTS
            ):
                if redis.execute_command("EVAL", _MOVE, 2, source, target, raw) != 1:
                    raise RuntimeError("queue_changed_during_stopped_migration")
                moved += 1
        for key in (source, target):
            after.update(
                _digest(raw)
                for raw in cast(list[bytes | str], redis.lrange(key, 0, -1))
            )
    if before != after:
        raise RuntimeError("queue_message_inventory_changed")
    return {
        "moved": moved,
        "messages_before": before.total(),
        "messages_after": after.total(),
    }


def _digest(raw: bytes | str) -> bytes:
    return sha256(raw.encode() if isinstance(raw, str) else raw).digest()
