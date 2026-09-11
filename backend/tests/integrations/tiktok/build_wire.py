"""双通道本地 HTTP 边界；读取与创建队列均使用真实 SDK/MCP HTTP。"""

import json
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from tests.integrations.tiktok.mcp_wire import McpWire


class BuildWire:
    operations = {
        "CAMPAIGN": "build.get_campaigns",
        "ADGROUP": "build.get_adgroups",
        "AD": "build.get_ads",
        "CTA": "build.get_cta_portfolio",
        "ADGROUP_STATUS": "build.get_regular_adgroups",
    }
    paths = {
        "CAMPAIGN": "/open_api/v1.3/smart_plus/campaign/get/",
        "ADGROUP": "/open_api/v1.3/smart_plus/adgroup/get/",
        "AD": "/open_api/v1.3/smart_plus/ad/get/",
        "CTA": "/open_api/v1.3/creative/portfolio/get/",
        "ADGROUP_STATUS": "/open_api/v1.3/adgroup/get/",
    }

    create_operations = {
        "CAMPAIGN": "build.create_campaign",
        "ADGROUP": "build.create_adgroup",
        "AD": "build.create_ad",
        "CTA": "build.create_cta_portfolio",
    }
    create_paths = {
        "CAMPAIGN": "/open_api/v1.3/smart_plus/campaign/create/",
        "ADGROUP": "/open_api/v1.3/smart_plus/adgroup/create/",
        "AD": "/open_api/v1.3/smart_plus/ad/create/",
        "CTA": "/open_api/v1.3/creative/portfolio/create/",
    }
    id_keys = {
        "CAMPAIGN": "campaign_id",
        "ADGROUP": "adgroup_id",
        "AD": "smart_plus_ad_id",
        "CTA": "creative_portfolio_id",
    }

    def __init__(self, channel):
        self.channel = channel
        self.calls = []
        self.results = defaultdict(deque)
        self.raw_results = defaultdict(deque)
        self.contracts = {
            contract.operation: contract
            for contract in load_tool_contracts()
            if contract.operation
            in {*self.operations.values(), *self.create_operations.values()}
        }
        self.mcp = None
        if channel == "OFFICIAL_MCP":
            self.mcp = McpWire(
                [
                    {"name": contract.tool_name, "inputSchema": contract.input_schema}
                    for contract in self.contracts.values()
                ]
            )
            self.endpoint = self.mcp.url
            self.calls = self.mcp.calls
            original_handler = self.mcp.server.RequestHandlerClass
            wire = self

            class RawEnvelopeHandler(original_handler):
                def respond(self, status, body=b"", headers=None):
                    # 仅扩展真实本地 HTTP 响应字节；数字字面量不经过 Python float。
                    if body and any(wire.raw_results.values()):
                        envelope = json.loads(body)
                        call = next(
                            (
                                item
                                for item in reversed(wire.calls)
                                if item.get("id") == envelope.get("id")
                                and item.get("method") == "tools/call"
                            ),
                            None,
                        )
                        tool = call["params"]["name"] if call else None
                        if wire.raw_results[tool]:
                            body = (
                                b'{"jsonrpc":"2.0","id":'
                                + json.dumps(envelope["id"]).encode()
                                + b',"result":{"content":[],"structuredContent":'
                                + wire.raw_results[tool].popleft()
                                + b"}}"
                            )
                    super().respond(status, body, headers)

            self.mcp.server.RequestHandlerClass = RawEnvelopeHandler
            return
        assert channel == "OFFICIAL_API"
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                target = urlsplit(self.path)
                arguments = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                wire.calls.append(
                    {"method": "POST", "path": target.path, "arguments": arguments}
                )
                kind = next(
                    (
                        kind
                        for kind, path in wire.create_paths.items()
                        if path == target.path
                    ),
                    None,
                )
                envelope = wire.results["CREATE_" + str(kind)].popleft()
                if envelope is None:
                    self.connection.shutdown(2)
                    self.connection.close()
                    return
                body = json.dumps(envelope).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                target = urlsplit(self.path)
                arguments = {}
                for key, values in parse_qs(target.query).items():
                    value = values[0]
                    arguments[key] = (
                        json.loads(value)
                        if key in {"fields", "filtering", "page", "page_size"}
                        else value
                    )
                wire.calls.append(
                    {"method": "GET", "path": target.path, "arguments": arguments}
                )
                kind = next(
                    (kind for kind, path in wire.paths.items() if path == target.path),
                    None,
                )
                envelope = (
                    wire.results[kind].popleft()
                    if wire.results[kind]
                    else {"code": 999, "data": {}}
                )
                body = (
                    envelope
                    if isinstance(envelope, bytes)
                    else json.dumps(envelope).encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def enqueue_created(self, kind, remote_id, *, status=None):
        data = {self.id_keys[kind]: remote_id}
        if status is not None:
            data["operation_status"] = status
        self.enqueue_create_envelope(
            kind, {"code": 0, "data": data, "request_id": "synthetic-create-request"}
        )

    def enqueue_create_envelope(self, kind, envelope):
        if self.mcp:
            tool = self.contracts[self.create_operations[kind]].tool_name
            self.mcp.results[tool].append(
                {"content": [], "structuredContent": envelope}
            )
        else:
            self.results["CREATE_" + kind].append(envelope)

    def drop_created_response(self, kind):
        if self.mcp:
            self.mcp.disconnect_after_accept(
                self.contracts[self.create_operations[kind]].tool_name
            )
        else:
            self.results["CREATE_" + kind].append(None)

    def enqueue_readback(self, kind, rows, page, total):
        data = (
            rows[0]
            if kind == "CTA" and len(rows) == 1
            else {
                "list": rows,
                "page_info": {
                    "page": page,
                    "page_size": 100,
                    "total_number": total,
                    "total_page": (total + 99) // 100,
                },
            }
        )
        self.enqueue_envelope(
            kind, {"code": 0, "data": data, "request_id": "synthetic-read-request"}
        )

    def enqueue_envelope(self, kind, envelope):
        if self.mcp:
            tool = self.contracts[self.operations[kind]].tool_name
            self.mcp.results[tool].append(
                {"content": [], "structuredContent": envelope}
            )
        else:
            self.results[kind].append(envelope)

    def enqueue_raw_envelope(self, kind, payload):
        if self.mcp:
            tool = self.contracts[self.operations[kind]].tool_name
            self.raw_results[tool].append(payload)
            self.enqueue_envelope(kind, {"code": 0, "data": {}})
        else:
            self.results[kind].append(payload)

    def business_calls(self):
        if self.mcp:
            return [
                {
                    "tool": call["params"]["name"],
                    "arguments": call["params"]["arguments"],
                }
                for call in self.calls
                if call.get("method") == "tools/call"
            ]
        return list(self.calls)

    def close(self):
        if self.mcp:
            self.mcp.close()
        else:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)


def sdk_build_operations(client):
    """SDK 合同测试的纯适配器入口；授权/准入另由真实 gateway 集成测试覆盖。"""
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from app.integrations.tiktok.adapters.sdk_builds import ApiBuildOperations

    deadline = datetime.now(UTC) + timedelta(seconds=40)

    @contextmanager
    def request_scope(advertiser_id, operation, actual_deadline):
        assert advertiser_id and operation.startswith("build.")
        assert actual_deadline == deadline
        yield

    return ApiBuildOperations(client, request_scope=request_scope, deadline=deadline)
