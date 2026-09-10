"""One maintenance page per tick, with a persistent, fenced scan cursor."""

from typing import Any
from uuid import uuid4

from sqlmodel import Session

from app.core.config import settings
from app.core.errors import DomainError

from .cleanup_reconcile import scan_abandoned_objects

LOCK_KEY = "materials:abandonment:lock:v1"
CURSOR_KEY = "materials:abandonment:cursor:v1"
SAVE_CURSOR = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '' then redis.call('del', KEYS[2])
else redis.call('set', KEYS[2], ARGV[2]) end
return 1
"""
RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def scan_abandonment_page(
    *, database_engine: Any, redis_client: Any, limit: int = 100
) -> int:
    if not settings.MATERIAL_CLEANUP_ENABLED:
        return 0
    owner = uuid4().hex
    if not redis_client.set(LOCK_KEY, owner, nx=True, ex=75):
        return 0
    try:
        cursor = redis_client.get(CURSOR_KEY)
        if isinstance(cursor, bytes):
            cursor = cursor.decode("utf-8")
        try:
            with Session(database_engine) as db, db.begin():
                result = scan_abandoned_objects(
                    db, cursor=cursor, limit=limit, enqueue=True
                )
        except DomainError as error:
            if error.code != "invalid_cursor":
                raise
            # A changed signing key invalidates the maintenance cursor, never
            # object ownership. The next tick restarts a bounded idempotent scan.
            redis_client.eval(SAVE_CURSOR, 2, LOCK_KEY, CURSOR_KEY, owner, "")
            return 0
        # Persist after the DB commit. A crash before this step safely replays
        # the page; a worker whose lock expired cannot rewind a later cursor.
        redis_client.eval(
            SAVE_CURSOR, 2, LOCK_KEY, CURSOR_KEY, owner, result["next_cursor"] or ""
        )
        return result["queued"]
    finally:
        redis_client.eval(RELEASE_LOCK, 1, LOCK_KEY, owner)
