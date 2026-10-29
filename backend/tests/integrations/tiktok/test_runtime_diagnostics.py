import json
import logging
from types import SimpleNamespace

import pytest

from app.core import diagnostics
from app.integrations.tiktok.request_diagnostics import trace_sdk_request


def events(caplog):
    return [
        json.loads(r.message.split(" ", 1)[1])
        for r in caplog.records
        if r.message.startswith("runtime_trace ")
    ]


def test_transport_failure_has_location_and_correlation_without_secret(
    caplog, monkeypatch
):
    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)

    def failed(*_args, **_kwargs):
        raise TimeoutError("token=private-token https://secret.example/signature")

    client = SimpleNamespace(request=failed)
    token = diagnostics.trace_context.set({"task_id": "test-task"})
    try:
        with (
            pytest.raises(TimeoutError),
            trace_sdk_request(
                client, operation="materials.upload_image_url", tenant_id="test-tenant"
            ),
        ):
            client.request("POST", "private-request-url")
    finally:
        diagnostics.trace_context.reset(token)
    assert client.request is failed
    record = events(caplog)[-1]
    assert record["event"] == "sdk_http_failed"
    assert record["task_id"] == "test-task"
    assert record["error_type"] == "TimeoutError"
    assert record["error_frames"]
    assert record["duration_ms"] >= 0
    assert "private-" not in caplog.text
    assert "signature" not in caplog.text


def test_response_receipt_metadata_survives_later_parser_error(caplog, monkeypatch):
    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    client = SimpleNamespace(
        request=lambda: SimpleNamespace(
            status=200,
            data=json.dumps(
                {
                    "code": 0,
                    "request_id": "platform-request",
                    "data": {"image_id": "platform-image", "image_url": "private-url"},
                }
            ).encode(),
        )
    )
    with (
        pytest.raises(ValueError),
        trace_sdk_request(client, operation="materials.upload_image_url"),
    ):
        client.request()
        raise ValueError("parser failed")
    result = events(caplog)[-1]
    assert result["event"] == "sdk_http_response"
    assert result["request_id"] == "platform-request"
    assert result["image_id"] == "platform-image"
    assert result["remote_code"] == 0
    assert "private-url" not in caplog.text


def test_task_queue_timing_and_context_do_not_leak_to_next_task(caplog, monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.jobs.diagnostics import finish_task, start_task

    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    now = datetime.now(UTC).timestamp()
    request = SimpleNamespace(
        headers={"diagnostic_published_at": now - 2, "diagnostic_due_at": now - 5},
        delivery_info={"routing_key": "builds"},
    )
    task = SimpleNamespace(name="builds.execute_step", request=request)
    step_id, tenant_id = str(uuid4()), str(uuid4())
    before = diagnostics.trace_context.get()
    start_task(
        task_id="dispatch-test",
        task=task,
        kwargs={
            "tenant_id": tenant_id,
            "payload": {"step_id": step_id, "url": "private-url"},
        },
    )
    finish_task(task=task, state="SUCCESS")
    started, finished = events(caplog)[-2:]
    assert started["step_id"] == finished["step_id"] == step_id
    assert 2000 <= started["broker_wait_ms"] < 4000
    assert started["due_to_start_ms"] >= 5000
    assert finished["duration_ms"] >= 0
    assert diagnostics.trace_context.get() is before
    assert "private-url" not in caplog.text
