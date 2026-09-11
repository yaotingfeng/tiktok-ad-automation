"""图片适配器使用真实SDK/MCP客户端，替身仅在最终HTTP边界。"""

import json
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from urllib3.response import HTTPResponse

from app.integrations.tiktok.adapters.mcp_materials import MCPMaterialOperations
from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
from app.integrations.tiktok.contracts.materials import RemoteCallBudget, URLImageUpload
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.sdk import official_client
from tests.integrations.tiktok.mcp_wire import McpWire


@pytest.fixture(params=["SDK", "MCP"])
def cover_case(request, monkeypatch):
    from app.integrations.tiktok.mcp import transport

    contracts = {
        c.operation: c
        for c in load_tool_contracts()
        if c.operation.startswith("materials.")
    }
    wire = McpWire(
        [
            {"name": c.tool_name, "inputSchema": c.input_schema}
            for c in contracts.values()
        ]
    )
    responses, calls, events = deque(), [], []
    deadline = datetime.now(UTC) + timedelta(seconds=40)
    budget = RemoteCallBudget(deadline, 45, 60000)

    def enqueue(operation, data, *, code=0):
        envelope = {"code": code, "data": data, "request_id": "image-receipt-request"}
        responses.append(envelope)
        wire.results[contracts[operation].tool_name].append(
            {"content": [], "structuredContent": envelope}
        )

    @contextmanager
    def scope(advertiser, operation, actual_deadline):
        assert actual_deadline == deadline
        events.append((advertiser, operation))
        yield

    def authorize(advertiser, operation):
        events.append((advertiser, operation))

    @contextmanager
    def admit(_advertiser, _operation):
        yield

    @contextmanager
    def opened(scopes=frozenset({6})):
        if request.param == "SDK":
            with official_client(access_token="synthetic-cover-token") as client:
                yield SDKMaterialOperations(
                    client, request_scope=scope, deadline=deadline, api_scope_ids=scopes
                )
        else:
            with open_bound_mcp_client(
                token="synthetic-cover-token",
                task_deadline=deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools={t["name"]: t for t in wire.tools},
            ) as client:
                yield MCPMaterialOperations(client)

    def http(pool, method, url, **kwargs):
        assert pool.retries.total == 0 and pool.retries.redirect == 0
        calls.append((method, url, kwargs))
        response = responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return HTTPResponse(body=json.dumps(response).encode(), status=200)

    class LocalTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, incoming):
            incoming.url = httpx2.URL(wire.url)
            return await self.inner.handle_async_request(incoming)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", http)
    monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
    try:
        yield request.param, opened, enqueue, budget, calls, wire, events
    finally:
        wire.close()


def test_actual_image_url_upload_yields_only_real_receipt(cover_case):
    channel, opened, enqueue, budget, calls, wire, events = cover_case
    enqueue(
        "materials.upload_image_url",
        {"image_id": "actual-target-image", "signature": "a" * 32},
    )
    with opened() as adapter:
        result = adapter.upload_image_url(
            URLImageUpload(
                "123",
                "https://cdn.example/target-cover?secret=signed",
                "stable-cover.jpg",
            ),
            budget=budget,
        )
    assert result.image_id == "actual-target-image" and result.signature == "a" * 32
    assert result.evidence.request_id == "image-receipt-request"
    assert ("123", "materials.upload_image_url") in events
    expected = {
        "advertiser_id": "123",
        "upload_type": "UPLOAD_BY_URL",
        "image_url": "https://cdn.example/target-cover?secret=signed",
        "file_name": "stable-cover.jpg",
    }
    if channel == "SDK":
        assert len(calls) == 1 and calls[0][0] == "POST"
        assert calls[0][2]["headers"]["Content-Type"] == "application/json"
        assert json.loads(calls[0][2]["body"]) == expected
    else:
        business = [c for c in wire.calls if c["method"] == "tools/call"]
        assert len(business) == 1 and business[0]["params"]["arguments"] == expected


@pytest.mark.parametrize(
    "image_id",
    [
        None,
        "",
        123,
        " target ",
        "id\nsecret",
        "https://cdn.example/private?token=secret",
        "//cdn.example/private",
        "data:private",
    ],
)
def test_invalid_or_url_image_id_never_becomes_receipt(cover_case, image_id):
    from app.integrations.tiktok.contracts.common import RemoteCallError

    channel, opened, enqueue, budget, calls, wire, _ = cover_case
    enqueue("materials.upload_image_url", {"image_id": image_id})
    with opened() as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_image_url(
                URLImageUpload("123", "https://cdn.example/cover", "stable.jpg"),
                budget=budget,
            )
    assert error.value.effect == "UNKNOWN"
    assert (
        len(
            calls
            if channel == "SDK"
            else [c for c in wire.calls if c["method"] == "tools/call"]
        )
        == 1
    )


@pytest.mark.parametrize("signature", [None, "bad", 123])
def test_optional_bad_signature_cannot_erase_real_image_id(cover_case, signature):
    _, opened, enqueue, budget, _, _, _ = cover_case
    enqueue(
        "materials.upload_image_url",
        {"image_id": "actual-image", "signature": signature},
    )
    with opened() as adapter:
        result = adapter.upload_image_url(
            URLImageUpload("123", "https://cdn.example/cover", "中文封面.jpg"),
            budget=budget,
        )
    assert result.image_id == "actual-image" and result.signature is None


@pytest.mark.parametrize("cover_case", ["SDK"], indirect=True)
@pytest.mark.parametrize(
    "scopes",
    [None, frozenset(), frozenset({61, 611}), frozenset({"6"}), frozenset({True}), {6}],
)
def test_api_image_upload_leaf_must_be_proven_before_http(cover_case, scopes):
    from app.integrations.tiktok.contracts.common import RemoteCallError

    _, opened, _, budget, calls, _, events = cover_case
    with opened(scopes) as adapter:
        with pytest.raises(RemoteCallError) as error:
            adapter.upload_image_url(
                URLImageUpload("123", "https://cdn.example/cover", "fixed.jpg"),
                budget=budget,
            )
    assert (
        error.value.effect == "NOT_SENT"
        and error.value.code == "cover_permission_unverified"
    )
    assert not calls and not events
