"""真实SDK传输边界故障：日志应直接说明阶段，不能依靠反查堆栈行号。"""

import json
import logging
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
import urllib3
from billiard.exceptions import SoftTimeLimitExceeded
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core import diagnostics
from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.request_diagnostics import trace_sdk_request
from app.integrations.tiktok.sdk import official_client
from app.integrations.tiktok.transport_diagnostics import (
    TransportTrace,
    observe_transport,
    safe_transport_summary,
)
from tests.integrations.tiktok.test_sdk_connection_phase import campaign


@contextmanager
def scope(*_args):
    yield


class Socket:
    def getpeername(self):
        return "203.0.113.9", 443

    def close(self):
        pass


@pytest.mark.parametrize(
    "phase", ["dns_tcp_connect", "tls_setup", "request_write", "response_read"]
)
def test_failure_logs_exact_stage_and_preserves_safe_evidence(
    phase, caplog, monkeypatch
):
    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    writes = []

    def new_conn(_self):
        if phase == "dns_tcp_connect":
            raise TimeoutError(
                "private-token https://private.example/?signature=secret"
            )
        return Socket()

    def connect(self):
        self.sock = self._new_conn()
        raise TimeoutError("private-token TLS timeout")

    def request(_self, *_args, **_kwargs):
        writes.append(True)
        if phase == "request_write":
            raise TimeoutError("private-token write timeout")

    def response(_self):
        raise TimeoutError("private-token response timeout")

    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "_new_conn", new_conn)
    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "connect", connect)
    if phase in {"request_write", "response_read"}:
        monkeypatch.setattr(
            urllib3.HTTPSConnectionPool, "_validate_conn", lambda *_args: None
        )
        monkeypatch.setattr(urllib3.connection.HTTPSConnection, "request", request)
        monkeypatch.setattr(urllib3.connection.HTTPSConnection, "getresponse", response)
    with official_client(access_token="private-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=40),
        )
        with trace_sdk_request(
            client,
            operation="build.create_campaign",
            tenant_id="tenant-test",
            advertiser_id="123",
            step_id="step-test",
        ):
            with pytest.raises(RemoteCallError) as caught:
                adapter.create(attempt_id=uuid4(), intent=campaign())
    rows = [
        json.loads(r.message.split(" ", 1)[1])
        for r in caplog.records
        if r.message.startswith("runtime_trace ")
    ]
    assert [row["event"] for row in rows] == ["sdk_http_started", "sdk_http_failed"]
    row = rows[-1]
    assert row["step_id"] == "step-test" and row["tenant_id"] == "tenant-test"
    transport = row["transport"]
    assert transport["failure_stage"] == phase
    assert transport["phase_ms"][phase] >= 0
    assert transport["connect_timeout_seconds"] == 5
    assert transport["read_timeout_seconds"] == 30
    assert len(transport["http_attempt_id"]) == 32
    assert caught.value.transport_diagnostics == transport
    if phase in {"dns_tcp_connect", "tls_setup"}:
        assert transport["send_state"] == "not_sent" and not writes
        assert caught.value.effect == "NOT_SENT"
    else:
        assert writes and caught.value.effect == "UNKNOWN"
        assert transport["send_state"] in {"may_have_sent", "write_completed"}
    assert "private-token" not in caplog.text and "private.example" not in caplog.text
    assert "signature" not in caplog.text and "Access-Token" not in caplog.text


@pytest.mark.filterwarnings("ignore:Unverified HTTPS request")
@pytest.mark.parametrize("diagnostic_error", [False, "summary", "elapsed"])
def test_real_local_https_success_keeps_sdk_behavior_and_phase_timings(
    tmp_path, caplog, monkeypatch, diagnostic_error
):
    if diagnostic_error:

        def broken_summary(*_args, **_kwargs):
            raise ValueError("diagnostic failure must not change SDK receipt")

        monkeypatch.setattr(TransportTrace, diagnostic_error, broken_summary)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(
                {
                    "code": 0,
                    "request_id": "platform-request",
                    "data": {
                        "campaign_id": "campaign-test",
                        "private_response": "never-log-me",
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_path), str(key_path))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = urllib3.PoolManager.request

    def redirect(pool, method, url, **kwargs):
        return original(
            pool,
            method,
            url.replace(
                "https://business-api.tiktok.com",
                f"https://127.0.0.1:{server.server_port}",
                1,
            ),
            **kwargs,
        )

    monkeypatch.setattr(urllib3.PoolManager, "request", redirect)
    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    try:
        with official_client(access_token="private-token") as client:
            client.rest_client.pool_manager.connection_pool_kw.update(
                cert_reqs="CERT_NONE", assert_hostname=False
            )
            adapter = ApiBuildOperations(
                client,
                request_scope=scope,
                deadline=datetime.now(UTC) + timedelta(seconds=40),
            )
            with trace_sdk_request(client, operation="build.create_campaign"):
                created = adapter.create(attempt_id=uuid4(), intent=campaign())
                adapter.create(attempt_id=uuid4(), intent=campaign())
        assert created.remote_id == "campaign-test" and len(calls) == 2
        rows = [
            json.loads(r.message.split(" ", 1)[1])
            for r in caplog.records
            if r.message.startswith("runtime_trace ")
        ]
        assert [r["event"] for r in rows] == [
            "sdk_http_started",
            "sdk_http_response",
        ] * 2
        if diagnostic_error == "summary":
            assert all(
                not row["transport"]
                for row in rows
                if row["event"] == "sdk_http_response"
            )
            return
        transport = rows[1]["transport"]
        assert transport["send_state"] == "write_completed"
        assert (
            transport["response_received"] is True
            and transport["connection_reused"] is False
        )
        assert transport["peer_ip"] == "127.0.0.1" and transport[
            "tls_version"
        ].startswith("TLS")
        if diagnostic_error == "elapsed":
            assert transport["phase_ms"] == {}
            return
        assert set(transport["phase_ms"]) >= {
            "dns_tcp_connect",
            "tls_setup",
            "request_write",
            "response_read",
        }
        reused = rows[3]["transport"]
        assert reused["connection_reused"] is True and reused["peer_ip"] == "127.0.0.1"
        assert "dns_tcp_connect" not in reused["phase_ms"]
        assert "never-log-me" not in caplog.text and "private-token" not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_persisted_transport_only_accepts_safe_whitelist():
    result = safe_transport_summary(
        {
            "http_attempt_id": "a" * 32,
            "failure_stage": "tls_setup",
            "send_state": "not_sent",
            "phase_ms": {
                "tls_setup": 5000,
                "private-token": 20,
                "request_write": float("nan"),
            },
            "runtime": {"pod": "builds-pod", "url": "https://private.example"},
            "headers": {"Access-Token": "private-token"},
            "error_frames": ["sdk.py:2:send", "https://private.example/secret"],
            "tls_version": [],
        }
    )
    assert result["phase_ms"] == {"tls_setup": 5000}
    assert result["error_frames"] == ["sdk.py:2:send"]
    assert "private" not in str(result) and "Access-Token" not in str(result)


@pytest.mark.parametrize("method", ["elapsed", "fail", "summary"])
def test_diagnostic_failure_cannot_replace_original_network_failure(
    method, monkeypatch
):
    def failed(*_args, **_kwargs):
        raise TimeoutError("original connection timeout")

    def broken(*_args, **_kwargs):
        raise ValueError("diagnostic failure")

    monkeypatch.setattr(urllib3.connection.HTTPSConnection, "_new_conn", failed)
    monkeypatch.setattr(TransportTrace, method, broken)
    with official_client(access_token="synthetic-token") as client:
        adapter = ApiBuildOperations(
            client,
            request_scope=scope,
            deadline=datetime.now(UTC) + timedelta(seconds=40),
        )
        with trace_sdk_request(client, operation="build.create_campaign"):
            with pytest.raises(RemoteCallError) as caught:
                adapter.create(attempt_id=uuid4(), intent=campaign())
        assert caught.value.effect == "NOT_SENT"


def test_transport_summary_rejects_unbounded_fields_and_scoped_ip():
    summary = safe_transport_summary(
        {
            "error_frames": ["sdk.py:1:" + "a" * 100000],
            "peer_ip": "fe80::1%private-token",
            "peer_port": 10**5000,
            "http_status": 999999,
            "errno": 10**5000,
            "runtime": {
                "transport_source_sha256": "not-a-hash",
                "build_revision": "private-token",
            },
        }
    )
    assert len(json.dumps(summary)) < 4096
    assert "private-token" not in str(summary)
    assert (
        not summary.get("peer_port")
        and not summary.get("http_status")
        and not summary.get("error_frames")
    )


@pytest.mark.parametrize(
    "interrupt", [SoftTimeLimitExceeded, KeyboardInterrupt, SystemExit]
)
def test_diagnostic_observer_preserves_worker_interrupts(interrupt):
    def stopped():
        raise interrupt()

    with pytest.raises(interrupt):
        observe_transport(stopped)
