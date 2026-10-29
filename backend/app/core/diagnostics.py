"""可关联的运行日志：只写身份、类型、代码位置和计时，不写异常正文/请求内容。"""

import json
import logging
import os
import re
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

logger = logging.getLogger("app.runtime_trace")
trace_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "runtime_trace", default=None
)
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_.:/-]{1,255}$")


def identifier(value: object) -> str | None:
    # 不接受 URL；平台图片 ID 中允许路径分隔符。
    return (
        value
        if isinstance(value, str)
        and "://" not in value
        and _IDENTIFIER.fullmatch(value)
        else None
    )


def error_fields(error: BaseException) -> dict[str, Any]:
    result: dict[str, Any] = {"error_type": type(error).__name__}
    result["error_frames"] = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)[-8:]
    ]
    for key in ("code", "effect", "admission_source", "admission_reason"):
        value = identifier(getattr(error, key, None))
        if value is not None:
            result["error_code" if key == "code" else key] = value
    # SQL 错误只保留 SQLSTATE；urllib3 的 reason 也只保留异常类型。
    cause = (
        getattr(error, "orig", None)
        or getattr(error, "reason", None)
        or error.__cause__
    )
    if isinstance(cause, BaseException):
        result["cause_type"] = type(cause).__name__
        state = getattr(cause, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            result["sqlstate"] = state
    evidence = getattr(error, "evidence", None)
    if evidence is not None:
        result["request_id"] = identifier(getattr(evidence, "request_id", None))
        remote_code = getattr(evidence, "remote_code", None)
        if type(remote_code) is int:
            result["remote_code"] = remote_code
    return result


def emit(event: str, **fields: Any) -> None:
    # 调用点只传显式白名单字段。日志 I/O 失败不能影响业务结果或准入释放。
    try:
        logger.info(
            "runtime_trace %s",
            json.dumps(
                {
                    **(trace_context.get() or {}),
                    "event": event,
                    "at": datetime.now(UTC).isoformat(),
                    "pid": os.getpid(),
                    **fields,
                },
                ensure_ascii=True,
                default=str,
            ),
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        pass


@contextmanager
def span(event: str, **fields: Any):
    started = monotonic()
    emit(event + "_started", **fields)
    try:
        yield
    except BaseException as error:
        emit(
            event + "_failed",
            **fields,
            duration_ms=round((monotonic() - started) * 1000, 3),
            **error_fields(error),
        )
        raise
    else:
        emit(
            event + "_finished",
            **fields,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
