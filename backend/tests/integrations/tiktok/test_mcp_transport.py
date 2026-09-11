import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.types import CallToolResult

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.mcp.transport import MAX_RESPONSE_BYTES
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS, AccountAdmissionDeferred


def test_disconnect_after_accept_does_not_replay(bound_client, mcp_wire):
    mcp_wire.disconnect_after_accept("fixture_create")
    with pytest.raises(RemoteCallError) as exc:
        bound_client.call(
            operation="builds.create_campaign",
            advertiser_id="123",
            arguments={"advertiser_id": "123"},
        )
    assert exc.value.effect == "UNKNOWN"
    calls = [c for c in mcp_wire.calls if c.get("method") == "tools/call"]
    assert len(calls) == 1


def invoke(client, **kwargs):
    values = {
        "operation": "builds.create_campaign",
        "advertiser_id": "123",
        "arguments": {"advertiser_id": "123"},
    }
    values.update(kwargs)
    return client.call(**values)


def tool_calls(wire):
    return [call for call in wire.calls if call["method"] == "tools/call"]


def test_real_handshake_catalog_call_close_each_admitted_once(client_factory, mcp_wire):
    with client_factory() as client:
        result = invoke(client)
        assert result.data == {"campaign_id": "456"}
        assert "synthetic-bearer-secret" not in repr(client)
    methods = [call["method"] for call in mcp_wire.calls]
    assert "server/discover" in methods
    assert "initialize" in methods
    assert "notifications/initialized" in methods
    assert methods.index("tools/list") < methods.index("tools/call")
    assert methods.count("tools/list") == 1
    assert methods[-1] == "close"
    entered = [event for event in client_factory.events if event[0] == "enter"]
    exited = [event for event in client_factory.events if event[0] == "exit"]
    authorized = [event for event in client_factory.events if event[0] == "authorize"]
    assert len(entered) == len(exited) == len(mcp_wire.calls)
    assert len(authorized) == 2 * len(mcp_wire.calls)
    assert sum(e[2] == "builds.create_campaign" for e in entered) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"advertiser_id": "999"},
        {"arguments": {"advertiser_id": "123", "unknown": 1}},
        {"operation": "arbitrary_remote_tool"},
        {"arguments": {"advertiser_id": 123}},
    ],
)
def test_invalid_input_never_sends(bound_client, mcp_wire, kwargs):
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client, **kwargs)
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_wrong_advertiser_is_authorized_at_boundary(client_factory, mcp_wire):
    with client_factory() as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client, advertiser_id="999", arguments={"advertiser_id": "999"})
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


@pytest.mark.parametrize("revoke_after", [1, 2])
def test_authorization_revoked_before_or_after_admission(
    client_factory, mcp_wire, revoke_after
):
    checked = 0
    leases = []

    def authorize(_advertiser_id, operation):
        nonlocal checked
        if operation == "builds.create_campaign":
            checked += 1
            if checked == revoke_after:
                raise RuntimeError(
                    "synthetic-bearer-secret https://signed.invalid/private"
                )

    @contextmanager
    def admit(_advertiser_id, operation):
        if operation == "builds.create_campaign":
            leases.append("enter")
        try:
            yield
        finally:
            if operation == "builds.create_campaign":
                leases.append("exit")

    with client_factory(authorize=authorize, admit=admit) as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert exc.value.__context__ is None
    assert not tool_calls(mcp_wire)
    assert leases == ([] if revoke_after == 1 else ["enter", "exit"])


def test_admission_failure_is_not_sent(client_factory, mcp_wire):
    @contextmanager
    def reject(_advertiser_id, operation):
        if operation == "builds.create_campaign":
            raise RuntimeError("quota rejected")
        yield

    with client_factory(admit=reject) as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_candidate_may_only_observe_catalog(client_factory, mcp_wire):
    with client_factory(observed_tools={}) as client:
        assert client.list_tools().tools[0].name == "fixture_create"
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_observed_schema_mismatch_not_sent(client_factory, mcp_wire):
    with client_factory(
        observed_tools={"fixture_create": {"name": "fixture_create", "inputSchema": {}}}
    ) as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_live_schema_drift_before_write_not_sent(client_factory, mcp_wire):
    with client_factory() as client:
        mcp_wire.tools = [{"name": "fixture_create", "inputSchema": {"type": "object"}}]
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_same_origin_redirect_cannot_replay_post(bound_client, mcp_wire, status):
    mcp_wire.redirects["fixture_create"] = status
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client)
    assert exc.value.effect == "UNKNOWN"
    assert len(tool_calls(mcp_wire)) == 1


def test_input_required_never_replays(client_factory, mcp_wire):
    mcp_wire.modern = True
    mcp_wire.results["fixture_create"].append(
        {
            "resultType": "input_required",
            "requestState": "continue",
            "inputRequests": {},
        }
    )
    with client_factory() as client:
        assert client._client.protocol_version == "2026-07-28"
        assert client._client.input_required_max_rounds == 0
        assert client._client.cache is None
        with pytest.raises(RemoteCallError) as exc:
            invoke(client)
    assert exc.value.effect == "UNKNOWN"
    assert len(tool_calls(mcp_wire)) == 1


@pytest.mark.parametrize("sse", [False, True])
def test_response_budget_is_enforced_on_physical_reads(bound_client, mcp_wire, sse):
    mcp_wire.sse = sse
    mcp_wire.enqueue_result(
        "fixture_create",
        CallToolResult(
            content=[],
            structured_content={"code": 0, "data": {"large": "x" * MAX_RESPONSE_BYTES}},
        ),
    )
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client)
    assert exc.value.effect == "UNKNOWN"
    assert len(tool_calls(mcp_wire)) == 1


def test_valid_sse_receipt(bound_client, mcp_wire):
    mcp_wire.sse = True
    assert invoke(bound_client).data == {"campaign_id": "456"}


def test_separate_clients_do_not_share_sessions_or_catalog(client_factory, mcp_wire):
    for _ in range(2):
        with client_factory() as client:
            invoke(client)
    assert len({c["session"] for c in tool_calls(mcp_wire)}) == 2
    assert sum(c["method"] == "tools/list" for c in mcp_wire.calls) == 2


def test_timeout_after_send_unknown_once_and_cannot_extend_task(
    client_factory, mcp_wire
):
    mcp_wire.delay = 3
    started = time.monotonic()
    with client_factory(
        task_deadline=datetime.now(UTC) + timedelta(seconds=0.65)
    ) as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client, deadline=datetime.now(UTC) + timedelta(seconds=30))
    assert exc.value.effect == "UNKNOWN"
    assert len(tool_calls(mcp_wire)) == 1
    assert time.monotonic() - started < 2


def test_per_call_deadline_can_only_shorten(bound_client, mcp_wire):
    mcp_wire.delay = 3
    started = time.monotonic()
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client, deadline=datetime.now(UTC) + timedelta(seconds=0.3))
    assert exc.value.effect == "UNKNOWN"
    assert time.monotonic() - started < 1.5
    assert len(tool_calls(mcp_wire)) == 1


def test_expired_or_naive_deadline_cannot_send(bound_client, mcp_wire):
    for deadline in [datetime.now(), datetime.now(UTC) - timedelta(seconds=1)]:
        with pytest.raises(RemoteCallError) as exc:
            invoke(bound_client, deadline=deadline)
        assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_deadline_rechecked_after_admission(client_factory, mcp_wire):
    @contextmanager
    def slow_admit(_advertiser_id, operation):
        if operation == "builds.create_campaign":
            time.sleep(0.2)
        yield

    with client_factory(admit=slow_admit) as client:
        with pytest.raises(RemoteCallError) as exc:
            invoke(client, deadline=datetime.now(UTC) + timedelta(seconds=0.15))
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_catalog_pagination_and_repeated_cursor(client_factory, mcp_wire):
    mcp_wire.pages = {
        None: {"tools": [], "nextCursor": "next"},
        "next": {"tools": mcp_wire.tools},
    }
    with client_factory(observed_tools={}) as client:
        page = client.list_tools()
        assert page.next_cursor == "next"
        assert (
            client.list_tools(cursor=page.next_cursor).tools[0].name == "fixture_create"
        )
    mcp_wire.pages["next"]["nextCursor"] = "next"
    with client_factory(observed_tools={}) as client:
        client.list_tools()
        with pytest.raises(RemoteCallError):
            client.list_tools(cursor="next")


def test_paginated_preload_avoids_post_write_catalog(client_factory, mcp_wire):
    mcp_wire.pages = {
        None: {"tools": [], "nextCursor": "next"},
        "next": {"tools": mcp_wire.tools},
    }
    with client_factory() as client:
        invoke(client)
    methods = [c["method"] for c in mcp_wire.calls]
    assert methods.count("tools/list") == 2
    assert "tools/list" not in methods[methods.index("tools/call") + 1 :]


def test_close_failure_preserves_returned_receipt(client_factory, mcp_wire):
    mcp_wire.close_failure = True
    with client_factory() as client:
        result = invoke(client)
    assert result.data == {"campaign_id": "456"}


def test_close_authorization_revoked_preserves_receipt(client_factory):
    def authorize(_advertiser_id, operation):
        if operation == "protocol.close":
            raise RuntimeError("revoked")

    with client_factory(authorize=authorize) as client:
        result = invoke(client)
    assert result.data == {"campaign_id": "456"}


def test_sensitive_wire_logs_are_disabled(client_factory, mcp_wire, caplog):
    with caplog.at_level(logging.DEBUG):
        with client_factory() as client:
            invoke(client)
    assert "synthetic-bearer-secret" not in caplog.text
    assert "business-api.tiktok.com" not in caplog.text
    assert mcp_wire.url not in caplog.text


def test_overlap_is_rejected(bound_client, mcp_wire):
    mcp_wire.delay = 0.3
    results = []
    worker = threading.Thread(target=lambda: results.append(invoke(bound_client)))
    worker.start()
    for _ in range(100):
        if tool_calls(mcp_wire):
            break
        time.sleep(0.005)
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client)
    worker.join(timeout=2)
    assert exc.value.effect == "NOT_SENT"
    assert results[0].data == {"campaign_id": "456"}
    assert len(tool_calls(mcp_wire)) == 1


def test_sync_client_rejects_asgi_loop(bound_client, mcp_wire):
    async def attempt():
        with pytest.raises(RemoteCallError) as exc:
            invoke(bound_client)
        assert exc.value.effect == "NOT_SENT"

    asyncio.run(attempt())
    assert not tool_calls(mcp_wire)


def test_production_rejects_arbitrary_endpoint(client_factory, mcp_wire):
    with pytest.raises(DomainError):
        with client_factory(endpoint=mcp_wire.url):
            pass
    assert not mcp_wire.calls


def test_401_never_refreshes_or_replays(bound_client, mcp_wire):
    mcp_wire.status = 401
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client)
    assert exc.value.effect == "UNKNOWN"
    assert len(tool_calls(mcp_wire)) == 1


def test_invalid_http_json_is_unknown_without_exception_body(bound_client, mcp_wire):
    mcp_wire.invalid_json = True
    with pytest.raises(RemoteCallError) as exc:
        invoke(bound_client)
    assert exc.value.effect == "UNKNOWN"
    assert exc.value.__context__ is None
    assert "synthetic-bearer-secret" not in str(exc.value)
    assert len(tool_calls(mcp_wire)) == 1


def test_unknown_result_retires_task_session(bound_client, mcp_wire):
    mcp_wire.disconnect_after_accept("fixture_create")
    with pytest.raises(RemoteCallError) as first:
        invoke(bound_client)
    with pytest.raises(RemoteCallError) as later:
        invoke(bound_client)
    assert first.value.effect == "UNKNOWN"
    assert later.value.effect == "NOT_SENT"
    assert len(tool_calls(mcp_wire)) == 1


def test_handshake_is_bounded_by_task_deadline(client_factory, mcp_wire):
    mcp_wire.handshake_delay = 3
    started = time.monotonic()
    with pytest.raises(RemoteCallError) as exc:
        with client_factory(task_deadline=datetime.now(UTC) + timedelta(seconds=0.2)):
            pytest.fail("handshake should time out")
    assert exc.value.effect == "NOT_SENT"
    assert time.monotonic() - started < 1.5
    assert not tool_calls(mcp_wire)


def test_catalog_size_limit_does_not_return_partial_page(client_factory, mcp_wire):
    mcp_wire.pages = {
        None: {
            "tools": [
                {
                    "name": "oversized",
                    "inputSchema": {},
                    "description": "x" * MAX_RESPONSE_BYTES,
                }
            ]
        }
    }
    with client_factory(observed_tools={}) as client:
        with pytest.raises(RemoteCallError) as exc:
            client.list_tools()
    assert exc.value.effect == "NOT_SENT"
    assert not tool_calls(mcp_wire)


def test_no_request_after_closed_scope(client_factory, mcp_wire):
    with client_factory() as client:
        invoke(client)
    count = len(mcp_wire.calls)
    with pytest.raises(RemoteCallError) as exc:
        invoke(client)
    assert exc.value.effect == "NOT_SENT"
    assert len(mcp_wire.calls) == count


def test_admission_cleanup_failure_preserves_validated_receipt(client_factory):
    @contextmanager
    def failing_close(_advertiser_id, operation):
        yield
        if operation == "builds.create_campaign":
            raise RuntimeError("lease close failed")

    with client_factory(admit=failing_close) as client:
        receipt = invoke(client)
    assert receipt.data == {"campaign_id": "456"}


@pytest.mark.parametrize("operation", ["builds.create_campaign", "protocol.discover"])
def test_local_admission_deferred_keeps_scheduler_signal(
    client_factory, mcp_wire, operation
):
    @contextmanager
    def reject(_advertiser_id, current_operation):
        if current_operation == operation:
            raise AccountAdmissionDeferred(731)
        yield

    with pytest.raises(AccountAdmissionDeferred) as exc:
        with client_factory(admit=reject) as client:
            invoke(client)
    assert exc.value.retry_after_ms == 731
    assert exc.value.retryable is True
    assert exc.value.__context__ is None
    assert not tool_calls(mcp_wire)
    if operation == "protocol.discover":
        assert not mcp_wire.calls


def test_local_allowlisted_domain_error_preserves_code_and_retryable(
    client_factory, mcp_wire
):
    def authorize(_advertiser_id, operation):
        if operation == "builds.create_campaign":
            raise DomainError(
                "admission_unconfigured", "synthetic-bearer-secret", retryable=True
            )

    with client_factory(authorize=authorize) as client:
        with pytest.raises(DomainError) as exc:
            invoke(client)
    assert type(exc.value) is DomainError
    assert exc.value.code == "admission_unconfigured"
    assert exc.value.retryable is True
    assert "synthetic-bearer-secret" not in str(exc.value)
    assert exc.value.__context__ is None
    assert not tool_calls(mcp_wire)


def test_modern_invalid_schema_cannot_leak_sdk_client_logs(
    client_factory, mcp_wire, caplog, monkeypatch
):
    monkeypatch.setattr(logging.getLogger("client"), "disabled", False)
    marker = "synthetic-sensitive-schema-marker"
    signed_url = "https://signed.invalid/private?signature=" + marker
    mcp_wire.modern = True
    mcp_wire.tools = [
        {
            "name": "unsafe-" + marker,
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string", "x-mcp-header": signed_url}},
            },
        }
    ]
    with caplog.at_level(logging.DEBUG):
        with client_factory(observed_tools={}) as client:
            assert client.list_tools().tools == ()
    assert marker not in caplog.text
    assert signed_url not in caplog.text


def test_unknown_retires_before_unlock_against_competing_write(
    client_factory, mcp_wire
):
    mcp_wire.enqueue_result(
        "fixture_create",
        CallToolResult(content=[], structured_content={"code": 9, "data": {}}),
    )
    released = threading.Event()
    resume = threading.Event()
    first_thread = None
    outcomes = []
    retired_at_release = []

    with client_factory() as client:
        real_lock = client._lock

        class PausedReleaseLock:
            def acquire(self, blocking=True):
                return real_lock.acquire(blocking=blocking)

            def release(self):
                if threading.current_thread() is first_thread:
                    retired_at_release.append(client._broken)
                    real_lock.release()
                    released.set()
                    assert resume.wait(2)
                else:
                    real_lock.release()

        client._lock = PausedReleaseLock()

        def first():
            try:
                invoke(client)
            except RemoteCallError as exc:
                outcomes.append(exc.effect)

        first_thread = threading.Thread(target=first)
        first_thread.start()
        try:
            assert released.wait(2)
            with pytest.raises(RemoteCallError) as exc:
                invoke(client)
            assert exc.value.effect == "NOT_SENT"
        finally:
            resume.set()
            first_thread.join(timeout=2)
    assert retired_at_release == [True]
    assert outcomes == ["UNKNOWN"]
    assert len(tool_calls(mcp_wire)) == 1


@pytest.mark.parametrize("interrupt_type", SDK_SCOPE_INTERRUPTS)
def test_main_thread_interrupt_reaches_actual_business_lease(
    client_factory, mcp_wire, redis_client, interrupt_type
):
    import signal
    from uuid import uuid4

    from app.jobs.admission import AdmissionPolicy, admission_keys, admitted_scope

    tenant_id = uuid4()
    scope = "mcp-interrupt-" + str(uuid4())
    operation = "builds.create_campaign"
    policy = AdmissionPolicy(
        app_max_inflight=10,
        endpoint_max_inflight=10,
        tenant_max_inflight=10,
        advertiser_max_inflight=10,
        app_calls_per_window=100,
        endpoint_calls_per_window=100,
        window_ms=1000,
        lease_ms=60000,
    )
    exits = []
    mcp_wire.delay = 1

    class BusinessLease:
        def __init__(self):
            self.inner = admitted_scope(
                redis_client,
                app_scope=scope,
                endpoint=operation,
                tenant_id=tenant_id,
                advertiser_id="123",
                policy=policy,
                denied_error=AccountAdmissionDeferred,
            )

        def __enter__(self):
            self.inner.__enter__()
            signal.setitimer(signal.ITIMER_REAL, 0.12)

        def __exit__(self, exc_type, exc, tb):
            exits.append(exc_type)
            return self.inner.__exit__(exc_type, exc, tb)

    @contextmanager
    def protocol_lease():
        yield

    def admit(_advertiser_id, current_operation):
        return BusinessLease() if current_operation == operation else protocol_lease()

    def interrupt(_signum, _frame):
        raise interrupt_type()

    previous = signal.signal(signal.SIGALRM, interrupt)
    keys = admission_keys(scope, operation, tenant_id, "123")
    try:
        with pytest.raises(RemoteCallError) as exc:
            with client_factory(admit=admit) as client:
                invoke(client)
        assert exc.value.effect == "UNKNOWN"
        assert exits == [interrupt_type]
        assert redis_client.zcard(keys[2]) == 1
        assert len(tool_calls(mcp_wire)) == 1
        count = len(mcp_wire.calls)
        time.sleep(0.15)
        assert len(mcp_wire.calls) == count
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        redis_client.delete(*keys)


def test_waiting_acquire_rechecks_retirement_under_lock(client_factory, mcp_wire):
    mcp_wire.delay = 0.2
    mcp_wire.enqueue_result(
        "fixture_create",
        CallToolResult(content=[], structured_content={"code": 9, "data": {}}),
    )
    reached_acquire = threading.Event()
    resume_acquire = threading.Event()
    outcomes = {}
    competitor = None

    with client_factory() as client:
        real_lock = client._lock

        class PausedAcquireLock:
            def acquire(self, blocking=True):
                if threading.current_thread() is competitor:
                    reached_acquire.set()
                    assert resume_acquire.wait(2)
                return real_lock.acquire(blocking=blocking)

            def release(self):
                real_lock.release()

        client._lock = PausedAcquireLock()

        def run(name):
            try:
                invoke(client)
            except RemoteCallError as exc:
                outcomes[name] = exc.effect
            else:
                outcomes[name] = "success"

        first = threading.Thread(target=run, args=("first",))
        competitor = threading.Thread(target=run, args=("competitor",))
        first.start()
        for _ in range(100):
            if tool_calls(mcp_wire):
                break
            time.sleep(0.005)
        competitor.start()
        try:
            assert reached_acquire.wait(2)
            first.join(timeout=2)
            assert not first.is_alive()
        finally:
            resume_acquire.set()
            competitor.join(timeout=2)
    assert outcomes == {"first": "UNKNOWN", "competitor": "NOT_SENT"}
    assert len(tool_calls(mcp_wire)) == 1
