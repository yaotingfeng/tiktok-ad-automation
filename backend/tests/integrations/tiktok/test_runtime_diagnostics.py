import json
import logging
from types import SimpleNamespace

import pytest

from app.core import diagnostics
from app.integrations.tiktok.request_diagnostics import trace_sdk_request
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


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


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize(
    "operation", ["ads.get_ads", "ads.get_campaigns", "reports.integrated"]
)
def test_directory_and_report_reads_trace_one_original_request_without_payload(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
    caplog,
    monkeypatch,
    operation,
):
    from datetime import date

    from app.integrations.tiktok.contracts.ads import DirectoryQuery
    from tests.integrations.tiktok.gateway_support import gateway

    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    gateway_wire["sdk_data"]["data"] = {
        "list": [],
        "page_info": {"page": 1, "page_size": 1000, "total_page": 1, "total_number": 0},
        "private_extra": "do-not-log-response-body",
    }
    with gateway(database_engine, redis_client, gateway_case) as client:
        official = client.ads._requests.client
        original = official.request
        original_override = "request" in official.__dict__
        if operation.startswith("ads."):
            client.ads.read_page(
                DirectoryQuery(
                    gateway_case[2],
                    "ad" if operation == "ads.get_ads" else "campaign",
                    "REGULAR",
                    1,
                    1000,
                    (),
                    (),
                    False,
                )
            )
        else:
            client.report_reader.ad_day(
                advertiser_ids=(gateway_case[2],),
                start=date(2026, 10, 5),
                end=date(2026, 10, 5),
                page=1,
            )
        assert official.request == original
        assert ("request" in official.__dict__) == original_override
    assert len(gateway_wire["sdk_calls"]) == 1
    records = [row for row in events(caplog) if row.get("operation") == operation]
    assert [row["event"] for row in records] == [
        "request_scope_started",
        "sdk_http_started",
        "sdk_http_response",
        "request_scope_finished",
    ]
    body = json.dumps(records)
    assert "do-not-log-response-body" not in body
    assert "synthetic-original-token" not in body
    assert "Access-Token" not in body
