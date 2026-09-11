"""双通道真实客户端和本地 HTTP；不替换 gateway 或 call_tool。"""

import json
import threading
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
import urllib3

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.materials import RemoteCallBudget
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.integrations.tiktok.mcp.transport import open_bound_mcp_client
from app.integrations.tiktok.sdk import official_client
from tests.integrations.tiktok.mcp_wire import McpWire


@pytest.fixture(params=["SDK", "MCP"])
def material_case(request, monkeypatch):
    from app.integrations.tiktok.adapters.mcp_materials import MCPMaterialOperations
    from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations
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
    replies, calls, events = deque(), [], []
    deadline = datetime.now(UTC) + timedelta(seconds=50)
    budget = RemoteCallBudget(deadline, 60, 75000)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            data = json.dumps(replies.popleft()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def enqueue(operation, data, code=0):
        envelope = {
            "code": 0 if code == "TEXT" else code,
            "request_id": "material-request",
            "data": data,
        }
        replies.append(envelope)
        wire.results[contracts[operation].tool_name].append(
            {"content": [{"type": "text", "text": json.dumps(envelope)}]}
            if code == "TEXT"
            else {"content": [], "structuredContent": envelope}
        )

    enqueue.channel = request.param

    def authorize(advertiser, operation):
        events.append((advertiser, operation))
        assert advertiser in (None, "123")

    @contextmanager
    def admit(_advertiser, _operation):
        yield

    @contextmanager
    def scope(advertiser, operation, actual_deadline):
        assert actual_deadline == deadline
        authorize(advertiser, operation)
        yield

    hosts = frozenset({"media.example.com"})
    try:
        if request.param == "SDK":
            original = urllib3.PoolManager.request

            def redirect(pool, method, url, **kwargs):
                assert url.startswith("https://business-api.tiktok.com/")
                local = f"http://127.0.0.1:{server.server_port}" + url.removeprefix(
                    "https://business-api.tiktok.com"
                )
                return original(pool, method, local, **kwargs)

            monkeypatch.setattr(urllib3.PoolManager, "request", redirect)
            with official_client(access_token="synthetic-token") as client:
                yield (
                    SDKMaterialOperations(
                        client,
                        request_scope=scope,
                        deadline=deadline,
                        preview_allowed_hosts=hosts,
                    ),
                    enqueue,
                    budget,
                    events,
                    calls,
                )
        else:

            class LocalTransport(httpx2.AsyncBaseTransport):
                def __init__(self):
                    self.inner = httpx2.AsyncHTTPTransport(retries=0)

                async def handle_async_request(self, request):
                    request.url = httpx2.URL(wire.url)
                    return await self.inner.handle_async_request(request)

                async def aclose(self):
                    await self.inner.aclose()

            monkeypatch.setattr(transport, "_new_http_transport", LocalTransport)
            with open_bound_mcp_client(
                token="synthetic-token",
                task_deadline=deadline,
                authorize=authorize,
                admit=admit,
                contracts=contracts,
                observed_tools={t["name"]: t for t in wire.tools},
            ) as client:
                yield (
                    MCPMaterialOperations(client, preview_allowed_hosts=hosts),
                    enqueue,
                    budget,
                    events,
                    wire.calls,
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        wire.close()


@pytest.mark.parametrize("images", [False, True])
def test_detail_preserves_large_string_id_missing_metadata_and_evidence(
    material_case, images
):
    adapter, enqueue, budget, events, _ = material_case
    identity = "90071992547409939999"
    operation = "materials.get_images" if images else "materials.get_videos"
    key = "image_id" if images else "video_id"
    enqueue(operation, {"list": [{key: identity, "unknown_secret": "must-disappear"}]})
    method = adapter.read_image if images else adapter.read_video
    row = method(advertiser_id="123", **{key: identity}, budget=budget)
    assert getattr(row, key) == identity
    assert row.width is None and row.evidence.request_id == "material-request"
    assert not hasattr(row, "unknown_secret")
    assert ("123", operation) in events
    if not images:
        assert row.md5 is None and row.mid is None


@pytest.mark.parametrize(
    "problem",
    [
        "numeric",
        "wrong_id",
        "other_account",
        "duplicate",
        "bad_signature",
        "bad_dimension",
        "missing_list",
        "business_error",
        "string_code",
    ],
)
def test_bad_video_response_cannot_become_identity(material_case, problem):
    adapter, enqueue, budget, _, _ = material_case
    row = {"video_id": "v"}
    data, code = {"list": [row]}, 0
    if problem == "numeric":
        row["video_id"] = 90071992547409939999
    elif problem == "wrong_id":
        row["video_id"] = "other"
    elif problem == "other_account":
        row["advertiser_id"] = "456"
    elif problem == "duplicate":
        data["list"].append(row)
    elif problem == "bad_signature":
        row["signature"] = "bad"
    elif problem == "bad_dimension":
        row["width"] = True
    elif problem == "missing_list":
        data = {}
    elif problem == "business_error":
        code = 40001
    else:
        code = "0"
    enqueue("materials.get_videos", data, code)
    with pytest.raises(DomainError):
        adapter.read_video(advertiser_id="123", video_id="v", budget=budget)


def test_empty_details_are_explicit_absence(material_case):
    adapter, enqueue, budget, _, _ = material_case
    for operation, method, key in [
        ("materials.get_videos", adapter.read_video, "video_id"),
        ("materials.get_images", adapter.read_image, "image_id"),
    ]:
        enqueue(operation, {"list": []})
        assert method(advertiser_id="123", **{key: "v"}, budget=budget) is None


def preview_row():
    return {
        "video_id": "v",
        "material_id": "mid",
        "signature": "B" * 32,
        "displayable": True,
        "preview_url": "https://media.example.com/v?signature=private",
        "width": 1080,
        "height": 1920,
        "size": 120,
        "duration": 4.5,
        "format": "mp4",
    }


def test_preview_preserves_actual_digest_and_safe_repr(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue("materials.get_videos", {"list": [preview_row()]})
    preview = adapter.read_source_preview(
        advertiser_id="123", video_id="v", budget=budget
    )
    assert preview.md5 == "b" * 32 and preview.evidence.request_id == "material-request"
    assert "signature=private" not in repr(preview) and preview.md5 not in repr(preview)


@pytest.mark.parametrize("problem", ["missing_digest", "host", "account"])
def test_preview_missing_actual_identity_or_trusted_host_fails(material_case, problem):
    adapter, enqueue, budget, _, _ = material_case
    row = preview_row()
    if problem == "missing_digest":
        del row["signature"]
    elif problem == "host":
        row["preview_url"] = "https://other.example.com/v"
    else:
        row["advertiser_id"] = "456"
    enqueue("materials.get_videos", {"list": [row]})
    with pytest.raises(DomainError):
        adapter.read_source_preview(advertiser_id="123", video_id="v", budget=budget)


@pytest.mark.parametrize("images", [False, True])
def test_search_returns_typed_page_and_actual_evidence(material_case, images):
    adapter, enqueue, budget, _, _ = material_case
    key = "image_id" if images else "video_id"
    operation = "materials.search_images" if images else "materials.search_videos"
    data = {
        "list": [{key: "v", "advertiser_id": "123"}],
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }
    enqueue(operation, data)
    page = (
        adapter.search_images(advertiser_id="123", page=1, budget=budget)
        if images
        else adapter.search_videos(
            advertiser_id="123", page=1, material_ids=("mid",), budget=budget
        )
    )
    assert page.total_number == 1 and len(page.rows) == 1
    assert page.evidence.request_id == "material-request"


@pytest.mark.parametrize(
    "problem", ["missing_page", "wrong_page", "duplicate", "count", "other_account"]
)
@pytest.mark.parametrize("images", [False, True])
def test_bad_search_pages_are_not_partial_success(material_case, problem, images):
    adapter, enqueue, budget, _, _ = material_case
    key = "image_id" if images else "video_id"
    data = {
        "list": [{key: "v"}],
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }
    if problem == "missing_page":
        del data["page_info"]
    elif problem == "wrong_page":
        data["page_info"]["page"] = 2
    elif problem == "duplicate":
        data["list"].append(data["list"][0])
        data["page_info"]["total_number"] = 2
    elif problem == "count":
        data["page_info"]["total_number"] = 2
    else:
        data["list"][0]["advertiser_id"] = "456"
    enqueue("materials.search_images" if images else "materials.search_videos", data)
    with pytest.raises(DomainError):
        if images:
            adapter.search_images(advertiser_id="123", page=1, budget=budget)
        else:
            adapter.search_videos(
                advertiser_id="123", page=1, material_ids=(), budget=budget
            )


def test_cross_page_duplicate_never_finishes_search(material_case):
    adapter, enqueue, budget, _, _ = material_case
    for page in (1, 2):
        enqueue(
            "materials.search_videos",
            {
                "list": [{"video_id": "v"}],
                "page_info": {"page": page, "page_size": 100, "total_page": 2},
            },
        )
    adapter.search_videos(advertiser_id="123", page=1, material_ids=(), budget=budget)
    with pytest.raises(DomainError):
        adapter.search_videos(
            advertiser_id="123", page=2, material_ids=(), budget=budget
        )


def test_cover_suggestion_is_not_an_image_receipt(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        "materials.get_suggested_covers",
        {
            "list": [
                {
                    "id": "not-image-id",
                    "url": "https://media.example.com/cover",
                    "width": 1080,
                    "height": 1920,
                }
            ]
        },
    )
    cover = adapter.suggest_cover(
        advertiser_id="123", video_id="v", width=1080, height=1920, budget=budget
    )
    assert cover.evidence.request_id == "material-request"
    assert not hasattr(cover, "image_id")


def test_unverified_writes_remain_not_sent(material_case):
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.integrations.tiktok.contracts.materials import (
        FileVideoUpload,
        URLImageUpload,
        URLVideoUpload,
    )

    adapter, _, budget, events, calls = material_case
    before = len(calls)
    writes = [
        (
            adapter.upload_video_url,
            URLVideoUpload(
                "123", "https://media.example.com/v", "fixed.mp4", "a" * 32, 120
            ),
        ),
        (
            adapter.upload_video_file,
            FileVideoUpload("123", "/synthetic/no-file", "fixed.mp4", "a" * 32, 120),
        ),
        (
            adapter.upload_image_url,
            URLImageUpload("123", "https://media.example.com/cover", "fixed.jpg"),
        ),
    ]
    from app.integrations.tiktok.adapters.sdk_materials import SDKMaterialOperations

    # P2.3开放原API视频写合同；图片留待P2.4，MCP无服务证据仍全部拒绝。
    if isinstance(adapter, SDKMaterialOperations):
        writes = writes[-1:]
    for method, request in writes:
        with pytest.raises(RemoteCallError) as error:
            method(request, budget=budget)
        assert (
            error.value.effect == "NOT_SENT"
            and error.value.code == "material_channel_unverified"
        )
    assert len(calls) == before and not any(
        op.startswith("materials.") for _, op in events
    )


def test_unverified_mcp_text_json_cannot_become_material_evidence(material_case):
    adapter, enqueue, budget, _, _ = material_case
    if enqueue.channel != "MCP":
        pytest.skip("text envelopes are MCP-specific")
    enqueue("materials.get_videos", {"list": [{"video_id": "v"}]}, "TEXT")
    with pytest.raises(DomainError):
        adapter.read_video(advertiser_id="123", video_id="v", budget=budget)


def test_video_cover_requires_actual_digest_and_returns_evidence(material_case):
    adapter, enqueue, budget, _, _ = material_case
    row = preview_row()
    row["video_cover_url"] = "https://media.example.com/cover"
    enqueue("materials.get_videos", {"list": [row]})
    cover = adapter.read_video_cover(
        advertiser_id="123", video_id="v", md5="b" * 32, budget=budget
    )
    assert cover.width == 1080 and cover.evidence.request_id == "material-request"
    enqueue("materials.get_videos", {"list": [row]})
    with pytest.raises(DomainError):
        adapter.read_video_cover(
            advertiser_id="123", video_id="v", md5="a" * 32, budget=budget
        )


def test_expired_budget_has_no_physical_material_call(material_case):
    from dataclasses import replace

    adapter, _, budget, _, calls = material_case
    before = len(calls)
    expired = replace(budget, deadline=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(DomainError):
        adapter.read_video(advertiser_id="123", video_id="v", budget=expired)
    assert len(calls) == before
