"""任务作用域的官方 MCP 客户端；HTTP 边界控制权限、准入及单次发送。"""

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from concurrent.futures import Future
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2
from anyio.from_thread import BlockingPortal, start_blocking_portal
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry

from app.core.errors import ERROR_HTTP_STATUS, DomainError
from app.core.logging import silence_mcp_wire_logs
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.mcp.protocol import (
    OFFICIAL_ENDPOINT,
    ToolContract,
    require_official_endpoint,
    verify_tool_schema,
)
from app.integrations.tiktok.mcp.results import decode_mcp_result
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS, AccountAdmissionDeferred
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PROTOCOL_OPERATIONS = frozenset(
    {
        "protocol.discover",
        "protocol.initialize",
        "protocol.initialized",
        "protocol.list_tools",
        "protocol.stream",
        "protocol.close",
        "protocol.cancel",
    }
)
_METHOD_OPERATIONS = {
    "server/discover": "protocol.discover",
    "initialize": "protocol.initialize",
    "notifications/initialized": "protocol.initialized",
    "tools/list": "protocol.list_tools",
    "notifications/cancelled": "protocol.cancel",
}
Authorize = Callable[[str | None, str], None]
Admit = Callable[[str | None, str], AbstractContextManager[None]]


def _error(code: str, *, sent: bool = False) -> RemoteCallError:
    return RemoteCallError(
        code, effect="UNKNOWN" if sent else "NOT_SENT", evidence=CallEvidence()
    )


def _remaining(deadline: datetime) -> float:
    if (
        not isinstance(deadline, datetime)
        or deadline.tzinfo is None
        or deadline.utcoffset() is None
    ):
        raise _error("mcp_deadline_invalid")
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise _error("mcp_deadline_exceeded")
    return remaining


def _require_sync() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise _error("mcp_sync_context_required")


def _new_http_transport() -> httpx2.AsyncBaseTransport:
    # 测试仅替换此传输边界；产品构造函数没有任意 URL 或 HTTP 客户端入口。
    return httpx2.AsyncHTTPTransport(retries=0)


@dataclass(frozen=True)
class ObservedToolPage:
    tools: tuple[Tool, ...]
    next_cursor: str | None


@dataclass
class _CallState:
    operation: str
    advertiser_id: str | None
    deadline: datetime
    tool_name: str | None = None
    sent: bool = False
    response_bytes: int = 0
    failure: RemoteCallError | None = None
    interruption: BaseException | None = None
    retired: bool = False
    send_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _exit_lease(
    lease: AbstractContextManager[None],
    state: _CallState | None,
    error: BaseException | None = None,
) -> None:
    # 中断由同步任务记录，真实 HTTP 租约稍后退出时仍须看到原中断类型。
    interruption = state.interruption if state is not None else None
    if interruption is None and isinstance(error, SDK_SCOPE_INTERRUPTS):
        interruption = error
    try:
        lease.__exit__(
            type(interruption) if interruption is not None else None, interruption, None
        )
    except BaseException as cleanup_error:
        if isinstance(cleanup_error, SDK_SCOPE_INTERRUPTS):
            if state is not None:
                state.interruption = cleanup_error
            raise
        # 普通清理错误不覆盖已取得的正文，业务回执仍由严格解码决定。


class _LimitedStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        stream: httpx2.AsyncByteStream,
        lease: AbstractContextManager[None],
        deadline: datetime,
        state: _CallState | None,
    ) -> None:
        self._stream = stream
        self._lease = lease
        self._deadline = deadline
        self._state = state
        self._size = 0
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with anyio.fail_after(_remaining(self._deadline)):
            async for chunk in self._stream:
                self._size += len(chunk)
                if self._state is not None:
                    self._state.response_bytes += len(chunk)
                if self._size > MAX_RESPONSE_BYTES or (
                    self._state is not None
                    and self._state.response_bytes > MAX_RESPONSE_BYTES
                ):
                    raise _error("mcp_response_too_large", sent=True)
                yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # 清理独立受剩余任务期限约束，不能在截止后重新开启一个完整预算。
            with anyio.move_on_after(
                max(0, (self._deadline - datetime.now(UTC)).total_seconds()),
                shield=True,
            ):
                await self._stream.aclose()
        except Exception:
            # 已读取的正文仍须通过严格解码；关闭错误不能覆盖该正文的业务回执。
            pass
        finally:
            _exit_lease(self._lease, self._state)


class _GuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, bound: BoundMCPClient) -> None:
        self._bound = bound
        self._inner = _new_http_transport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        bound = self._bound
        state = bound._active
        business = False
        lease = None
        entered = False
        try:
            require_official_endpoint(str(request.url))
            if request.method == "POST":
                message = json.loads(request.content)
                method = message.get("method")
                if method == "tools/call":
                    business = True
                    if (
                        state is None
                        or state.tool_name != message["params"]["name"]
                        or state.sent
                    ):
                        raise _error(
                            "mcp_replay_blocked", sent=bool(state and state.sent)
                        )
                    operation, advertiser_id = state.operation, state.advertiser_id
                else:
                    operation, advertiser_id = _METHOD_OPERATIONS[method], None
                    # 官方 SDK 不得在已发送写入后隐式补取目录。
                    if method == "tools/list" and state is not None and state.sent:
                        raise _error("mcp_catalog_after_send", sent=True)
            elif request.method in ("GET", "DELETE"):
                operation = (
                    "protocol.stream" if request.method == "GET" else "protocol.close"
                )
                advertiser_id = None
            else:
                raise _error("mcp_protocol_request_invalid")
            deadline = state.deadline if state is not None else bound._task_deadline
            _remaining(deadline)
            try:
                bound._authorize(advertiser_id, operation)
                _remaining(deadline)
                lease = bound._admit(advertiser_id, operation)
                lease.__enter__()
                entered = True
                _remaining(deadline)
                bound._authorize(advertiser_id, operation)
                _remaining(deadline)
            except Exception as callback_error:
                if state is None or not state.sent:
                    # 仅本地发送前回调可传递调度语义，不信任远端错误类别或正文。
                    if isinstance(callback_error, AccountAdmissionDeferred):
                        bound._callback_error = AccountAdmissionDeferred(
                            callback_error.retry_after_ms
                        )
                    elif (
                        isinstance(callback_error, DomainError)
                        and callback_error.code in ERROR_HTTP_STATUS
                    ):
                        bound._callback_error = DomainError(
                            callback_error.code,
                            "TikTok 调用前检查未通过",
                            retryable=callback_error.retryable,
                        )
                raise
            if business and state is not None:
                # 与同步调用的退役决定共用发送锁；中断后尚未发送的后台任务不能补发。
                with state.send_guard:
                    if state.retired or bound._broken or bound._closed:
                        raise _error("mcp_session_unavailable")
                    state.sent = True
            with anyio.fail_after(_remaining(deadline)):
                response = await self._inner.handle_async_request(request)
            # SDK 自己跟随同源 307/308，必须在它看到响应之前拦截，而非只配 follow_redirects。
            if (
                300 <= response.status_code < 400
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                with anyio.move_on_after(_remaining(deadline), shield=True):
                    await response.aclose()
                raise _error("mcp_http_response_rejected", sent=business)
            # SSE 也逐块累计；禁止把超限截断结果交给上层当成完整回执。
            if not isinstance(response.stream, httpx2.AsyncByteStream):
                raise _error("mcp_protocol_response_invalid", sent=business)
            response.stream = _LimitedStream(
                response.stream,
                lease,
                deadline,
                state
                if business
                or (state is not None and state.sent and request.method == "GET")
                else None,
            )
            return response
        except BaseException as exc:
            if state is not None and isinstance(exc, SDK_SCOPE_INTERRUPTS):
                state.interruption = exc
            if entered and lease is not None:
                _exit_lease(lease, state, exc)
            if not isinstance(exc, Exception):
                raise
            if business and state is not None:
                state.failure = _error("mcp_request_failed", sent=state.sent)
            failure = _error("mcp_request_failed", sent=bool(state and state.sent))
        # 不保留 HTTP 异常、回调异常或 URL 的异常链。
        raise failure

    async def aclose(self) -> None:
        with anyio.move_on_after(
            max(0, (self._bound._task_deadline - datetime.now(UTC)).total_seconds()),
            shield=True,
        ):
            await self._inner.aclose()


class BoundMCPClient:
    """仅由 open_bound_mcp_client 创建；不跨任务、进程或租户共享会话。"""

    def __init__(
        self,
        *,
        task_deadline: datetime,
        authorize: Authorize,
        admit: Admit,
        contracts: Mapping[str, ToolContract],
        observed_tools: Mapping[str, dict[str, Any]],
    ):
        self._task_deadline = task_deadline
        self._authorize = authorize
        self._admit = admit
        self._contracts = deepcopy(dict(contracts))
        self._observed = deepcopy(dict(observed_tools))
        self._portal: BlockingPortal | None = None
        self._client: Client | None = None
        self._active: _CallState | None = None
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._catalog_ready = False
        self._cursors: set[str] = set()
        self._visited_cursors: set[str | None] = set()
        self._closed = False
        self._broken = False
        self._callback_error: DomainError | None = None
        self._session_scope: anyio.CancelScope | None = None

    def _acquire(self) -> None:
        _require_sync()
        if not self._lock.acquire(blocking=False):
            raise _error("mcp_call_overlap")
        try:
            # 可用性检查与调用退役使用同一把锁，防止先检查后等待的竞争调用穿透。
            if self._closed or self._broken or os.getpid() != self._pid:
                raise _error("mcp_session_unavailable")
            _remaining(self._task_deadline)
        except BaseException:
            self._lock.release()
            raise

    def _cancel_session(self) -> None:
        if self._session_scope is not None:
            self._session_scope.cancel()

    def _abort_pending(self, future: Future[Any] | None) -> None:
        if future is not None:
            future.cancel()
        try:
            # call_tool 的等待任务与 SDK HTTP writer 不同；取消整个会话才能停止后台请求。
            self._blocking_portal().call(self._cancel_session)
        except BaseException:
            pass

    def _session_client(self) -> Client:
        if self._client is None:
            raise _error("mcp_session_unavailable")
        return self._client

    def _blocking_portal(self) -> BlockingPortal:
        if self._portal is None:
            raise _error("mcp_session_unavailable")
        return self._portal

    async def _list_page(self, cursor: str | None) -> ObservedToolPage:
        if cursor in self._visited_cursors:
            raise _error("mcp_catalog_cursor_repeated")
        self._visited_cursors.add(cursor)
        page = await self._session_client().list_tools(cursor=cursor)
        if page.next_cursor is not None:
            if page.next_cursor == cursor or page.next_cursor in self._cursors:
                raise _error("mcp_catalog_cursor_repeated")
            self._cursors.add(page.next_cursor)
        return ObservedToolPage(tuple(page.tools), page.next_cursor)

    async def _preload_catalog(self) -> None:
        if self._catalog_ready:
            return
        self._cursors.clear()
        self._visited_cursors.clear()
        observed = {}
        cursor = None
        while True:
            page = await self._list_page(cursor)
            for tool in page.tools:
                if tool.name in observed:
                    raise _error("mcp_catalog_duplicate_tool")
                observed[tool.name] = tool.model_dump(by_alias=True, exclude_none=True)
            cursor = page.next_cursor
            if cursor is None:
                break
        for contract in self._contracts.values():
            verify_tool_schema(contract, observed.get(contract.tool_name, {}))
        self._catalog_ready = True

    async def _perform(
        self, state: _CallState, arguments: dict[str, Any]
    ) -> McpBusinessResponse:
        with anyio.fail_after(_remaining(state.deadline)):
            await self._preload_catalog()
            if state.tool_name is None:
                raise _error("mcp_tool_unavailable")
            result = await self._session_client().call_tool(state.tool_name, arguments)
            return decode_mcp_result(result, contract=self._contracts[state.operation])

    def call(
        self,
        *,
        operation: str,
        advertiser_id: str | None,
        arguments: dict[str, Any],
        deadline: datetime | None = None,
    ) -> McpBusinessResponse:
        self._acquire()
        state = None
        future: Future[McpBusinessResponse] | None = None
        self._callback_error = None
        try:
            try:
                effective = self._task_deadline
                if deadline is not None:
                    _remaining(deadline)
                    effective = min(effective, deadline)
                state = _CallState(operation, advertiser_id, effective)
                contract = self._contracts.get(operation)
                if (
                    contract is None
                    or contract.operation != operation
                    or not self._observed
                ):
                    raise _error("mcp_tool_unavailable")
                verify_tool_schema(contract, self._observed.get(contract.tool_name, {}))
                if type(arguments) is not dict:
                    raise _error("mcp_arguments_invalid")
                snapshot = json.loads(json.dumps(arguments, allow_nan=False))
                if snapshot.get("advertiser_id") != advertiser_id:
                    raise _error("mcp_advertiser_mismatch")
                validator = validator_for(contract.input_schema)
                validator.check_schema(contract.input_schema)
                validator(contract.input_schema, registry=Registry()).validate(snapshot)
                state.tool_name = contract.tool_name
                self._active = state
                future = self._blocking_portal().start_task_soon(
                    self._perform, state, snapshot
                )
                result = future.result()
            except BaseException as exc:
                if state is not None:
                    with state.send_guard:
                        state.retired = True
                        if isinstance(exc, SDK_SCOPE_INTERRUPTS):
                            state.interruption = exc
                        sent = state.sent
                else:
                    sent = False
                if (
                    sent
                    or isinstance(exc, SDK_SCOPE_INTERRUPTS)
                    or (future is not None and not future.done())
                ):
                    # 必须在解锁之前退役；主线程中断也不能遗留可继续发送的 portal 任务。
                    self._broken = True
                    self._abort_pending(future)
                if self._callback_error is not None and not sent:
                    failure = self._callback_error
                elif isinstance(exc, RemoteCallError):
                    failure = RemoteCallError(
                        exc.code,
                        effect="UNKNOWN" if sent else exc.effect,
                        evidence=exc.evidence,
                    )
                elif state is not None and state.failure is not None:
                    failure = state.failure
                else:
                    failure = _error("mcp_call_failed", sent=sent)
            else:
                return result
            # 保存的回调错误也在锁内处理；不在解锁后重读其他调用可变的共享字段。
            raise failure
        finally:
            self._active = None
            self._lock.release()

    def list_tools(self, *, cursor: str | None = None) -> ObservedToolPage:
        self._acquire()
        self._callback_error = None
        future = None
        try:
            try:
                if cursor is not None and (
                    not isinstance(cursor, str)
                    or not cursor
                    or cursor not in self._cursors
                ):
                    raise _error("mcp_catalog_cursor_invalid")
                if cursor is None:
                    self._cursors.clear()
                    self._visited_cursors.clear()
                self._catalog_ready = False

                async def perform() -> ObservedToolPage:
                    with anyio.fail_after(_remaining(self._task_deadline)):
                        return await self._list_page(cursor)

                future = self._blocking_portal().start_task_soon(perform)
                result = future.result()
            except BaseException as exc:
                if isinstance(exc, SDK_SCOPE_INTERRUPTS) or (
                    future is not None and not future.done()
                ):
                    self._broken = True
                    self._abort_pending(future)
                failure = self._callback_error or _error("mcp_catalog_failed")
            else:
                return result
            raise failure
        finally:
            self._lock.release()


@asynccontextmanager
async def _session(bound: BoundMCPClient, token: str) -> AsyncIterator[BoundMCPClient]:
    with anyio.fail_after(_remaining(bound._task_deadline)) as session_scope:
        bound._session_scope = session_scope
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            timeout=httpx2.Timeout(10.0, read=30.0),
            follow_redirects=False,
            transport=_GuardedTransport(bound),
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(
                OFFICIAL_ENDPOINT, http_client=http_client
            )
            async with Client(
                transport, input_required_max_rounds=0, cache=None
            ) as client:
                bound._client = client
                yield bound


@contextmanager
def open_bound_mcp_client(
    *,
    token: str,
    task_deadline: datetime,
    authorize: Authorize,
    admit: Admit,
    contracts: Mapping[str, ToolContract],
    observed_tools: Mapping[str, dict[str, Any]],
    endpoint: str = OFFICIAL_ENDPOINT,
) -> Iterator[BoundMCPClient]:
    """同步 prefork 任务入口。token 只传给内存 HTTP header，不进入对象 repr。"""
    _require_sync()
    _remaining(task_deadline)
    require_official_endpoint(endpoint)
    if (
        not isinstance(token, str)
        or not token
        or any(char in token for char in ("\r", "\n"))
    ):
        raise _error("mcp_credentials_invalid")
    silence_mcp_wire_logs()
    bound = BoundMCPClient(
        task_deadline=task_deadline,
        authorize=authorize,
        admit=admit,
        contracts=contracts,
        observed_tools=observed_tools,
    )
    with start_blocking_portal(name="tiktok-mcp-task") as portal:
        bound._portal = portal
        manager = portal.wrap_async_context_manager(_session(bound, token))
        try:
            manager.__enter__()
        except Exception:
            failed = True
        else:
            failed = False
        if failed:
            bound._closed = True
            if bound._callback_error is not None:
                raise bound._callback_error
            raise _error("mcp_session_open_failed")
        try:
            yield bound
        finally:
            bound._closed = True
            try:
                manager.__exit__(None, None, None)
            except Exception:
                # 回执已交给业务层持久化；清理故障不得把它改写成可重建失败。
                pass
            # 上层可能已取得 UNKNOWN；退出任务后不能再留有独立 portal 业务等待任务。
            try:
                portal.call(portal.stop, True)
            except RuntimeError:
                pass
            bound._client = None
            bound._portal = None
