"""Short fair turns cover quota admission, never SDK network calls."""

from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from redis.exceptions import RedisError

from app.core.errors import DomainError

SCRIPT = Path(__file__).with_name("fair_turn.lua").read_text()


def fair_keys(app_scope: str) -> list[str]:
    if not isinstance(app_scope, str) or not app_scope.strip():
        raise DomainError("admission_unconfigured", "应用调用额度尚未配置")
    base = f"tiktok:{{{sha256(app_scope.encode()).hexdigest()}}}:build-fair"
    return [
        f"{base}:{part}" for part in ("ready", "turn", "sequence", "seen", "delayed")
    ]


def _execute(redis_client: Any, keys: list[str], args: list[str | int]) -> bool:
    try:
        return bool(redis_client.eval(SCRIPT, len(keys), *keys, *args))
    except RedisError:
        raise DomainError(
            "admission_unavailable", "调度服务暂不可用", retryable=True
        ) from None


def take_fair_turn(
    redis_client: Any,
    *,
    app_scope: str,
    tenant_id: UUID,
    owner: UUID,
    wait_ms: int,
    turn_ms: int,
) -> bool:
    if (
        any(type(n) is not int or n <= 0 for n in (wait_ms, turn_ms))
        or turn_ms > wait_ms
    ):
        raise DomainError("admission_policy_invalid", "公平调度时间配置无效")
    return _execute(
        redis_client,
        fair_keys(app_scope),
        ["take", str(tenant_id), str(owner), wait_ms, turn_ms, 0, 0],
    )


def finish_fair_turn(
    redis_client: Any,
    *,
    app_scope: str,
    tenant_id: UUID,
    owner: UUID,
    served: bool,
    retry_after_ms: int = 0,
) -> None:
    if (
        type(served) is not bool
        or type(retry_after_ms) is not int
        or retry_after_ms < 0
    ):
        raise DomainError("admission_policy_invalid", "公平调度结果无效")
    _execute(
        redis_client,
        fair_keys(app_scope),
        [
            "finish",
            str(tenant_id),
            str(owner),
            30000,
            3000,
            1 if served else 0,
            retry_after_ms,
        ],
    )
