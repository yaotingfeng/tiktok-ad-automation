"""Shared remote-object coordination for every TikTok mutation path.

The lock is deliberately held in Redis rather than by a SQL row transaction.  A
worker may therefore keep its mutation lease while it is waiting on the
provider without holding a database row lock.  Keys omit BC and connection:
the same advertiser object is one remote object even when two bindings point at
it.  Parent campaign keys make Smart+ series operations intersect with child
operations and with the legacy build isolation path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.models import AdObject

_DEFAULT_LEASE_SECONDS = 120
_LOCK_PREFIX = "tiktok:mutation:"


@dataclass(frozen=True)
class MutationLease:
    """A fenced ownership token for a set of remote object keys."""

    owner_id: UUID
    token: str
    generation: int
    keys: tuple[str, ...]
    expires_at: datetime
    lease_seconds: int = _DEFAULT_LEASE_SECONDS
    _redis: Redis | None = None

    def __enter__(self) -> MutationLease:
        self.assert_current()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()

    def is_current(self, redis_client: Redis | None = None) -> bool:
        client = redis_client or self._redis or _redis()
        script = """
        local expected = ARGV[1]
        for _, key in ipairs(KEYS) do
          if redis.call('get', key) ~= expected then return 0 end
        end
        return 1
        """
        try:
            expected = f"{self.owner_id}:{self.token}:{self.generation}"
            return bool(client.eval(script, len(self.keys), *self.keys, expected))
        finally:
            if redis_client is None and self._redis is None:
                client.close()

    def assert_current(self, redis_client: Redis | None = None) -> None:
        if not self.is_current(redis_client):
            raise DomainError(
                "mutation_lease_stale", "mutation_lease_stale: 远端对象领取代数已失效"
            )

    def fence(self, redis_client: Redis | None = None) -> None:
        """Atomically validate and renew the token immediately before send."""
        client = redis_client or self._redis or _redis()
        script = """
        local expected = ARGV[1]
        for _, key in ipairs(KEYS) do
          if redis.call('get', key) ~= expected then return 0 end
        end
        for _, key in ipairs(KEYS) do
          redis.call('pexpire', key, ARGV[2])
        end
        return 1
        """
        expected = f"{self.owner_id}:{self.token}:{self.generation}"
        try:
            ok = client.eval(
                script,
                len(self.keys),
                *self.keys,
                expected,
                self.lease_seconds * 1000,
            )
        finally:
            if redis_client is None and self._redis is None:
                client.close()
        if not ok:
            raise DomainError(
                "mutation_lease_stale", "mutation_lease_stale: 远端对象领取代数已失效"
            )

    def release(self, redis_client: Redis | None = None) -> None:
        client = redis_client or self._redis or _redis()
        script = """
        local released = 0
        for _, key in ipairs(KEYS) do
          if redis.call('get', key) == ARGV[1] then
            redis.call('del', key)
            released = released + 1
          end
        end
        return released
        """
        expected = f"{self.owner_id}:{self.token}:{self.generation}"
        try:
            client.eval(script, len(self.keys), *self.keys, expected)
        finally:
            if redis_client is None and self._redis is None:
                client.close()


def _redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _key(ref: EntityRef) -> str:
    return f"{_LOCK_PREFIX}{ref.tenant_id}:{ref.advertiser_id}:{ref.kind}:{ref.remote_id}"


def _refs_with_ancestors(session: Session, refs: Iterable[EntityRef]) -> tuple[EntityRef, ...]:
    """Expand local parent observations into a stable, deduplicated lock set."""
    found: dict[tuple[UUID, str, str, str], EntityRef] = {}
    pending = list(refs)
    while pending:
        ref = pending.pop()
        identity = (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id)
        if identity in found:
            continue
        found[identity] = ref
        row = session.get(
            AdObject,
            (ref.tenant_id, ref.advertiser_id, ref.kind, ref.remote_id),
        )
        if row is not None and row.parent_ref is not None:
            pending.append(row.parent_ref)
    return tuple(found[key] for key in sorted(found, key=lambda value: tuple(map(str, value))))


def claim_mutation(
    session: Session,
    refs: tuple[EntityRef, ...],
    owner_id: UUID,
    *,
    redis_client: Redis | None = None,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> MutationLease:
    """Atomically claim all overlapping objects, returning a fenced lease.

    Redis is the admission authority and PostgreSQL is only used for a short,
    non-locking ancestor lookup.  Sorting keys gives callers deterministic
    acquisition order and the Lua script prevents a partial claim.
    """
    if not refs or not isinstance(owner_id, UUID):
        raise ValueError("mutation refs and owner are required")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 960:
        raise ValueError("invalid mutation lease")
    if any(ref.tenant_id != refs[0].tenant_id for ref in refs):
        raise DomainError("mutation_scope_invalid", "一次领取不能跨租户")
    keys = tuple(_key(ref) for ref in _refs_with_ancestors(session, refs))
    client = redis_client or _redis()
    token = uuid4().hex
    # Check all keys and set all of them in one Redis operation.  The generation
    # counter survives expiry, fencing a worker that wakes up after lease loss.
    script = """
    for _, key in ipairs(KEYS) do
      if redis.call('exists', key) == 1 then return {0} end
    end
    local result = {}
    local generation = redis.call('incr', ARGV[3])
    for _, key in ipairs(KEYS) do
      redis.call('set', key, ARGV[1] .. ':' .. generation, 'EX', ARGV[2])
    end
    return {generation}
    """
    try:
        generation_key = f"{_LOCK_PREFIX}{refs[0].tenant_id}:generation"
        result = client.eval(
            script,
            len(keys),
            *keys,
            f"{owner_id}:{token}",
            lease_seconds,
            generation_key,
        )
        if not result or result[0] == 0:
            raise DomainError(
                "mutation_busy", "mutation_busy: 远端对象正在由其他任务管理"
            )
        generation = int(result[0])
        return MutationLease(
            owner_id=owner_id,
            token=token,
            generation=generation,
            keys=keys,
            expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
            lease_seconds=lease_seconds,
            _redis=redis_client,
        )
    finally:
        if redis_client is None:
            # The lease keeps no connection open; subsequent checks create a
            # bounded client as needed.  Redis keys themselves hold ownership.
            client.close()


__all__ = ["MutationLease", "claim_mutation"]
