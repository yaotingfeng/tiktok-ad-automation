"""连接尚未校验成功与请求已发出的失败必须给出不同副作用结论。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from ssl import SSLCertVerificationError
from uuid import uuid4

import pytest
import urllib3

from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.execution_state import transient_retry
from app.modules.builds.request_compiler import decode_intent


@contextmanager
def scope(*_args):
    yield


def campaign():
    return decode_intent(
        "CAMPAIGN",
        {
            "advertiser_id": "123",
            "campaign_name": "connection-phase-test",
            "budget": "100",
            "operation_status": "ENABLE",
            "objective_type": "APP_PROMOTION",
            "app_promotion_type": "MINIS",
            "campaign_type": "REGULAR_CAMPAIGN",
            "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
            "budget_optimize_on": True,
        },
    )


def test_https_validation_timeout_is_not_sent_and_does_not_enter_request(monkeypatch):
    sent = []

    def validate(_self, _conn):
        raise TimeoutError("synthetic handshake timeout")

    def request(_self, *_args, **_kwargs):
        sent.append(True)
        raise AssertionError("HTTP request must not start after failed TLS validation")

    monkeypatch.setattr(urllib3.HTTPSConnectionPool, "_validate_conn", validate)
    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "request", request)
    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=campaign())
        assert caught.value.effect == "NOT_SENT"
        assert caught.value.code == "tiktok_connection_not_sent"
        assert not sent


def test_timeout_after_connection_validation_remains_unknown(monkeypatch):
    sent = []

    def request(_self, *_args, **_kwargs):
        sent.append(True)
        raise TimeoutError("synthetic request write timeout")

    monkeypatch.setattr(
        urllib3.HTTPSConnectionPool, "_validate_conn", lambda *args: None
    )
    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "request", request)
    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=campaign())
        assert caught.value.effect == "UNKNOWN"
        assert len(sent) == 1


def test_connection_guard_does_not_modify_other_pool_managers():
    original = urllib3.PoolManager().pool_classes_by_scheme["https"]
    with official_client(access_token="synthetic-token") as client:
        assert (
            client.rest_client.pool_manager.pool_classes_by_scheme["https"]
            is not original
        )
    assert urllib3.PoolManager().pool_classes_by_scheme["https"] is original


def test_response_read_timeout_remains_unknown(monkeypatch):
    sent = []

    def request(_self, *_args, **_kwargs):
        sent.append("request")

    def response(_self):
        sent.append("response")
        raise TimeoutError("synthetic response timeout")

    monkeypatch.setattr(
        urllib3.HTTPSConnectionPool, "_validate_conn", lambda *args: None
    )
    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "request", request)
    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "getresponse", response)
    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=campaign())
        assert caught.value.effect == "UNKNOWN" and sent == ["request", "response"]


def test_certificate_failure_is_not_sent_but_not_transient(monkeypatch):
    def validate(_self, _conn):
        raise SSLCertVerificationError(1, "synthetic certificate rejected")

    monkeypatch.setattr(urllib3.HTTPSConnectionPool, "_validate_conn", validate)
    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        )
        with pytest.raises(RemoteCallError) as caught:
            adapter.create(attempt_id=uuid4(), intent=campaign())
        assert caught.value.effect == "NOT_SENT"
        assert caught.value.code == "tiktok_connection_setup_failed"
        retry, _, state = transient_retry(
            {}, error=caught.value, retryable=False, delay=15
        )
        assert retry is False and "transport_failure_count" not in state


def test_confirmed_connection_timeout_uses_existing_three_attempt_budget():
    from app.integrations.tiktok.contracts.common import CallEvidence

    state = {"frozen_intent": {"campaign_name": "unchanged"}}
    error = RemoteCallError(
        "tiktok_connection_not_sent", effect="NOT_SENT", evidence=CallEvidence()
    )
    observed = []
    for _ in range(3):
        retry, delay, state = transient_retry(
            state, error=error, retryable=False, delay=15
        )
        observed.append((retry, delay))
    assert observed == [(True, 5), (True, 10), (False, 20)]
    assert state["transport_failure_count"] == 3
    assert state["frozen_intent"] == {"campaign_name": "unchanged"}


def test_read_only_connection_failures_keep_original_exception_contract(monkeypatch):
    def validate(_self, _conn):
        raise TimeoutError("synthetic handshake timeout")

    monkeypatch.setattr(urllib3.HTTPSConnectionPool, "_validate_conn", validate)
    with official_client(access_token="synthetic-token") as client:
        pool = client.rest_client.pool_manager
        with pytest.raises(urllib3.exceptions.MaxRetryError) as caught:
            pool.request(
                "GET",
                "https://example.invalid/",
                timeout=urllib3.Timeout(connect=1, read=1),
            )
        assert isinstance(caught.value.reason, urllib3.exceptions.ReadTimeoutError)


def test_previous_failed_connection_cannot_authorize_later_sent_request_retry(
    monkeypatch,
):
    def validate(_self, _conn):
        raise TimeoutError("first request failed before HTTP")

    def request(_self, *_args, **_kwargs):
        raise TimeoutError("second request may have written its body")

    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        )
        monkeypatch.setattr(urllib3.HTTPSConnectionPool, "_validate_conn", validate)
        try:
            adapter.create(attempt_id=uuid4(), intent=campaign())
        except RemoteCallError as first:
            assert first.effect == "NOT_SENT"
            monkeypatch.setattr(
                urllib3.HTTPSConnectionPool, "_validate_conn", lambda *_args: None
            )
            monkeypatch.setattr(urllib3.connection.HTTPSConnection, "request", request)
            with pytest.raises(RemoteCallError) as second:
                adapter.create(attempt_id=uuid4(), intent=campaign())
            assert second.value.effect == "UNKNOWN"
        else:
            pytest.fail("The first validation must fail")
