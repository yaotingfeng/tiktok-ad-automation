"""合成能力证据只验证本地合同；真实 SDK/MCP 请求仅在 HTTP 边界替身。"""

import json
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from urllib3.response import HTTPResponse

from app.integrations.tiktok.adapters.mcp_materials import MCPMaterialOperations
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.materials import (
    FileVideoUpload,
    RemoteCallBudget,
    URLVideoUpload,
)
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.sdk import official_client
from app.modules.materials.channel_policy import MaterialUploadPolicy
from tests.integrations.tiktok.mcp_wire import McpWire
from tests.modules.materials.test_url_sdk_contract import multipart_fields

# 仅离线测试明确赋予的服务合同，不代表生产API/MCP已核实。
SYNTHETIC_POLICY = MaterialUploadPolicy(1024)
REQUEST = URLVideoUpload(
    "123",
    "https://approved-cdn.example/video.mp4?signature=secret",
    "stable.mp4",
    "a" * 32,
    120,
)


@pytest.fixture(params=["SDK", "MCP"])
def upload_case(request, monkeypatch):
    from app.integrations.tiktok.mcp import transport

    contracts = {
        c.operation: c
        for c in load_tool_contracts()
        if c.operation == "materials.upload_video_url"
    }
    wire = McpWire(
        [
            {"name": c.tool_name, "inputSchema": c.input_schema}
            for c in contracts.values()
        ]
    )
    replies, http_calls, admissions = deque(), [], []
    deadline = datetime.now(UTC) + timedelta(seconds=50)
    budget = RemoteCallBudget(deadline, 60, 75000)

    def enqueue(data, *, code=0):
        envelope = {"code": code, "request_id": "upload-request", "data": data}
        replies.append(envelope)
        wire.results["file_video_ad_upload"].append(
            {"content": [], "structuredContent": envelope}
        )

    def authorize(advertiser, operation):
        assert advertiser in (None, "123")
        admissions.append((advertiser, operation))

    @contextmanager
    def admit(_advertiser, _operation):
        yield

    @contextmanager
    def scope(advertiser, operation, actual_deadline):
        assert actual_deadline == deadline
        authorize(advertiser, operation)
        yield

    @contextmanager
    def opened(policy=SYNTHETIC_POLICY):
        if request.param == "SDK":
            with official_client(access_token="synthetic-token") as client:
                yield SDKMaterialOperations(
                    client, request_scope=scope, deadline=deadline
                )
        else:
            with open_bound_mcp_client(
                token="synthetic-token",
                task_deadline=deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools={t["name"]: t for t in wire.tools},
            ) as client:
                yield MCPMaterialOperations(client, upload_policy=policy)

    def sdk_http(pool, method, url, **kwargs):
        assert pool.retries.total == 0 and pool.retries.redirect == 0
        http_calls.append((method, url, kwargs))
        return HTTPResponse(body=json.dumps(replies.popleft()).encode(), status=200)

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, incoming):
            incoming.url = httpx2.URL(wire.url)
            return await self.inner.handle_async_request(incoming)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", sdk_http)
    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
    try:
        yield request.param, opened, enqueue, budget, http_calls, wire, admissions
    finally:
        wire.close()


def test_url_upload_returns_actual_receipt_and_uses_channel_specific_fields(
    upload_case,
):
    channel, opened, enqueue, budget, calls, wire, admissions = upload_case
    row = {"video_id": "actual-vid", "material_id": "actual-mid"}
    enqueue([row] if channel == "SDK" else row)
    with opened() as adapter:
        receipt = adapter.upload_video_url(REQUEST, budget=budget)
        assert (receipt.video_id, receipt.mid) == ("actual-vid", "actual-mid")
        assert receipt.evidence.request_id == "upload-request"
    assert ("123", "materials.upload_video_url") in admissions
    if channel == "SDK":
        assert len(calls) == 1
        fields = multipart_fields(calls[0])
        assert fields["video_signature"] == "a" * 32
        assert fields["auto_fix_enabled"] == fields["auto_bind_enabled"] == "False"
    else:
        business = [c for c in wire.calls if c["method"] == "tools/call"]
        assert len(business) == 1
        assert business[0]["params"]["arguments"] == {
            "advertiser_id": "123",
            "file_name": "stable.mp4",
            "upload_type": "UPLOAD_BY_URL",
            "video_url": REQUEST.url,
            "auto_fix_enabled": False,
            "auto_bind_enabled": False,
        }


@pytest.mark.parametrize("upload_case", ["MCP"], indirect=True)
def test_unverified_policy_does_not_send_upload(upload_case):
    _, opened, _, budget, calls, wire, _ = upload_case
    with opened(MaterialUploadPolicy(None)) as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_video_url(REQUEST, budget=budget)
    assert error.value.code == "material_channel_unverified"
    assert error.value.effect == "NOT_SENT"
    assert calls == [] and not [c for c in wire.calls if c["method"] == "tools/call"]


def test_nonzero_business_response_stays_unknown_without_replay(upload_case):
    channel, opened, enqueue, budget, calls, wire, _ = upload_case
    enqueue({} if channel == "MCP" else [], code=40001)
    with opened() as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_video_url(REQUEST, budget=budget)
    assert error.value.effect == "UNKNOWN"
    assert (
        len(
            calls
            if channel == "SDK"
            else [c for c in wire.calls if c["method"] == "tools/call"]
        )
        == 1
    )


@pytest.mark.parametrize("upload_case", ["MCP"], indirect=True)
def test_mcp_file_upload_rejects_before_accessing_local_original(upload_case, tmp_path):
    channel, opened, _, budget, calls, wire, _ = upload_case
    path = tmp_path / "must-not-be-read.mp4"
    with opened() as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_video_file(
                FileVideoUpload("123", str(path), "stable.mp4", "a" * 32, 120),
                budget=budget,
            )
    assert error.value.code == "material_channel_unverified"
    assert error.value.effect == "NOT_SENT"
    assert (
        not path.exists()
        and calls == []
        and not [c for c in wire.calls if c["method"] == "tools/call"]
    )


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"material_id": "mid"},
        {"video_id": None},
        {"video_id": 123},
        {"video_id": " "},
        {"video_id": "vid", "material_id": 123},
        {"video_id": "vid", "advertiser_id": "another-account"},
    ],
)
def test_receipt_rejects_missing_invalid_or_foreign_actual_identity(upload_case, row):
    channel, opened, enqueue, budget, calls, wire, _ = upload_case
    enqueue([row] if channel == "SDK" else row)
    with opened() as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_video_url(REQUEST, budget=budget)
    assert error.value.effect == "UNKNOWN"
    assert (
        len(
            calls
            if channel == "SDK"
            else [c for c in wire.calls if c["method"] == "tools/call"]
        )
        == 1
    )


@pytest.mark.parametrize("upload_case", ["MCP"], indirect=True)
@pytest.mark.parametrize("field", ["auto_fix_enabled", "auto_bind_enabled"])
def test_observed_schema_must_support_both_explicit_false_fields(upload_case, field):
    from copy import deepcopy

    _, opened, _, budget, _, wire, _ = upload_case
    schema = deepcopy(wire.tools[0]["inputSchema"])
    schema["properties"].pop(field)
    wire.tools[0]["inputSchema"] = schema
    with opened() as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_video_url(REQUEST, budget=budget)
    assert error.value.effect == "NOT_SENT"
    assert not [c for c in wire.calls if c["method"] == "tools/call"]
