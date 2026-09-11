"""固定认证端点的一次性有界 POST；不注册、不重试、不保留远端正文。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2

from app.core.errors import DomainError
from app.core.logging import silence_mcp_wire_logs

MAX_OAUTH_BYTES = 64 * 1024


def _new_oauth_transport() -> httpx2.AsyncBaseTransport:
    # 离线测试只替换此 HTTP 边界，授权状态机与令牌校验保持真实执行。
    return httpx2.AsyncHTTPTransport(retries=0)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def strict_json(data: bytes) -> dict[str, Any]:
    def reject_constant(_: str) -> None:
        raise ValueError("nonfinite JSON number")

    value = json.loads(
        data, object_pairs_hook=_unique_pairs, parse_constant=reject_constant
    )
    if type(value) is not dict:
        raise ValueError("expected object")
    return value


async def _exchange(
    *,
    endpoint: str,
    form: dict[str, str],
    task_deadline: datetime,
    on_received: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    remaining = (task_deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise DomainError("mcp_oauth_result_unknown", "授权兑换时限已过")
    silence_mcp_wire_logs()
    failure = "mcp_oauth_result_unknown"
    received = None
    try:
        # 异步取消覆盖 DNS、连接、响应体和清理，而非仅设置单次 socket 超时。
        with anyio.fail_after(remaining):
            async with httpx2.AsyncClient(
                transport=_new_oauth_transport(),
                follow_redirects=False,
                trust_env=False,
                timeout=httpx2.Timeout(min(10.0, remaining)),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as client:
                async with client.stream("POST", endpoint, data=form) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("content-encoding", "identity")
                        != "identity"
                    ):
                        raise ValueError("unexpected token response")
                    if (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .lower()
                        != "application/json"
                    ):
                        raise ValueError("unexpected token representation")
                    body = bytearray()
                    # aiter_bytes 在 EOF 自动关闭响应；直接读取 identity 流，
                    # 让调用者先持久化完整回执，再进入 response/client 清理。
                    if not isinstance(response.stream, httpx2.AsyncByteStream):
                        raise ValueError("unexpected token stream")
                    async for chunk in response.stream:
                        body.extend(chunk)
                        if len(body) > MAX_OAUTH_BYTES:
                            raise ValueError("token response too large")
                    value = strict_json(bytes(body))
                    if "error" in value:
                        raise ValueError("token rejected")
                    if on_received is not None:
                        on_received(value)
                    # 持久化回调失败时保持 None，不能把已取得正文误报为成功。
                    received = value
    except ValueError, TypeError:
        failure = "mcp_token_response_invalid"
    except Exception:
        pass
    if received is not None:
        return received
    # 在 except 外创建安全异常，避免 OAuth URL/token 挂在 __context__ 上。
    raise DomainError(failure, "授权兑换未完成，请重新发起授权")


def exchange_token(
    *,
    endpoint: str,
    form: dict[str, str],
    task_deadline: datetime,
    on_received: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        return await _exchange(
            endpoint=endpoint,
            form=form,
            task_deadline=task_deadline,
            on_received=on_received,
        )

    return anyio.run(run)
