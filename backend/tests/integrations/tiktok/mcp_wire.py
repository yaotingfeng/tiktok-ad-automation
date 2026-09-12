"""本地真实 HTTP MCP 协议替身；从不访问 TikTok 或使用真实凭据。"""

import json
import socket
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from mcp.types import CallToolResult


class McpWire:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []
        self.stopping = threading.Event()
        self.results = defaultdict(deque)
        self.disconnects = set()
        self.redirects = {}
        self.delay = 0
        self.close_failure = False
        self.sse = False
        self.modern = False
        self.status = 200
        self.invalid_json = False
        self.handshake_delay = 0
        self.close_connections = False
        self.pages = {}
        wire = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                self.connection.settimeout(1)
                try:
                    super().handle()
                except ConnectionResetError, BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

            def respond(self, status, body=b"", headers=None):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if wire.close_connections:
                    # 多进程验收的本地服务显式结束连接，避免空闲超时与下次发送竞争。
                    self.send_header("Connection", "close")
                    self.close_connection = True
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError, ConnectionResetError:
                    pass

            def do_GET(self):
                wire.calls.append({"http_method": "GET", "method": "stream"})
                self.respond(405)

            def do_DELETE(self):
                wire.calls.append(
                    {
                        "http_method": "DELETE",
                        "method": "close",
                        "session": self.headers.get("mcp-session-id"),
                    }
                )
                self.respond(500 if wire.close_failure else 204)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body["http_method"] = "POST"
                body["session"] = self.headers.get("mcp-session-id")
                wire.calls.append(body)
                method = body["method"]
                if method == "server/discover":
                    if wire.handshake_delay:
                        wire.stopping.wait(wire.handshake_delay)
                    if wire.modern:
                        envelope = {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {
                                "supportedVersions": ["2026-07-28"],
                                "capabilities": {"tools": {}},
                            },
                        }
                        self.respond(
                            200,
                            json.dumps(envelope).encode(),
                            {"Content-Type": "application/json"},
                        )
                        return
                    envelope = {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "legacy fixture"},
                    }
                    self.respond(
                        200,
                        json.dumps(envelope).encode(),
                        {"Content-Type": "application/json"},
                    )
                    return
                if method.startswith("notifications/"):
                    self.respond(202)
                    return
                headers = {"Content-Type": "application/json"}
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "local-fixture", "version": "1"},
                    }
                    headers["mcp-session-id"] = str(uuid4())
                elif method == "tools/list":
                    cursor = body.get("params", {}).get("cursor")
                    result = wire.pages.get(cursor, {"tools": wire.tools})
                    if wire.modern:
                        result = {
                            **result,
                            "cacheScope": "private",
                            "ttlMs": 0,
                            "resultType": "complete",
                        }
                elif method == "tools/call":
                    tool = body["params"]["name"]
                    if tool in wire.disconnects:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if tool in wire.redirects:
                        self.respond(
                            wire.redirects[tool],
                            headers={"Location": wire.url + "/redirected"},
                        )
                        return
                    if wire.delay:
                        wire.stopping.wait(wire.delay)
                    result = (
                        wire.results[tool].popleft()
                        if wire.results[tool]
                        else {
                            "content": [],
                            "structuredContent": {
                                "code": 0,
                                "data": {"campaign_id": "456"},
                            },
                        }
                    )
                else:
                    result = {}
                envelope = json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}
                ).encode()
                if wire.sse and method == "tools/call":
                    headers["Content-Type"] = "text/event-stream"
                    envelope = b"event: message\ndata: " + envelope + b"\n\n"
                if method == "tools/call" and wire.invalid_json:
                    envelope = b"not JSON synthetic-bearer-secret https://signed.invalid/private"
                self.respond(
                    wire.status if method == "tools/call" else 200, envelope, headers
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def enqueue_result(self, tool: str, result: CallToolResult):
        self.results[tool].append(result.model_dump(by_alias=True, exclude_none=True))

    def disconnect_after_accept(self, tool: str):
        self.disconnects.add(tool)

    def close(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
