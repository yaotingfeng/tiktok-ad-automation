from collections.abc import Mapping
from math import isfinite
from uuid import UUID

from app.core.errors import ERROR_HTTP_STATUS

_ID_FIELDS = {"tenant_id", "actor_id", "task_id", "step_id", "request_id"}


def log_fields(values: Mapping[str, object]) -> dict[str, str | int | float]:
    """Keep only bounded, typed operational metadata, never request/provider data."""
    result: dict[str, str | int | float] = {}
    for key, value in values.items():
        if key in _ID_FIELDS and isinstance(value, str | UUID):
            try:
                result[key] = str(UUID(str(value)))
            except ValueError:
                pass
        elif key == "error_code" and isinstance(value, str):
            if value in ERROR_HTTP_STATUS or value == "internal_error":
                result[key] = value
        elif (
            key == "duration_ms"
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            if isfinite(value) and value >= 0:
                result[key] = value
    return result


def silence_mcp_wire_logs() -> None:
    """官方传输可能记录完整 URL、header、SSE 与异常正文；禁用其原始日志。"""
    import logging

    prefixes = ("mcp", "httpx2", "httpcore", "httpcore2")
    # mcp 2.2.0 session.py 使用独立的 client logger，不属于 mcp 命名空间。
    names = (
        {"client"}
        | set(prefixes)
        | {
            name
            for name in logging.Logger.manager.loggerDict
            if any(
                name == prefix or name.startswith(prefix + ".") for prefix in prefixes
            )
        }
    )
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
