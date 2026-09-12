"""Linux-only real hard kill and late server effect; macOS skip is not evidence."""

# ruff: noqa: F401,F811 -- real isolated PG/Redis/channel fixtures

import io
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from uuid import uuid4

import httpx2
import pytest
import urllib3
from celery import Celery
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from app.integrations.tiktok.admission import quota_scope
from app.jobs.admission import admission_keys
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TikTokConnection
from app.modules.builds import execution, reconciliation
from app.modules.builds.execution import _require_bounded_worker as worker_guard
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.execution_state import expire_attempt
from tests.modules.builds.test_channel_execution import (
    app_config,
    channel_execution,
    created,
    database_engine,
    gateway_case,
    gateway_wire,
    invoke,
    policy,
    retain_build_history,
    scene_case,
)


@pytest.fixture(autouse=True)
def independent_http_connections(gateway_wire):
    # 多次数据库鉴权之间不复用测试服务仅保留一秒的空闲连接；MCP 会话仍按原协议验证。
    gateway_wire["wire"].close_connections = True


def read_diagnostic(database_engine, step_id, reads):
    """只报告本地状态和调用次数，避免超时失败掩盖具体核查原因。"""
    with Session(database_engine) as db:
        step = db.get(ExecutionStep, step_id)
        return step.status, step.error_code, step.phase, len(reads)


@pytest.mark.skipif(
    sys.platform != "linux", reason="actual prefork hard-kill validation requires Linux"
)
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_hard_kill_late_effect_is_read_on_original_connection_without_second_create(
    channel_execution, database_engine, redis_client, monkeypatch
):
    from app.integrations.tiktok.mcp import transport
    from app.integrations.tiktok.mcp.protocol import load_mcp_protocol

    case, wire = channel_execution
    context, route = case["context"], case["route"]
    created(wire, "CTA")
    assert invoke(database_engine, redis_client, case, "CTA") == "SUCCEEDED", (
        read_diagnostic(database_engine, case["ids"]["CTA"], []),
        [call["method"] for call in wire["wire"].calls],
    )
    step_id = case["ids"]["CAMPAIGN"]
    received, release = threading.Event(), threading.Event()
    creates, reads, effects = [], [], []
    original_handler = wire["wire"].server.RequestHandlerClass

    class Handler(original_handler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            mcp = route.channel == "OFFICIAL_MCP"
            operation = body.get("params", {}).get("name") if mcp else self.path
            is_create = operation == (
                "smart_plus_campaign_create"
                if mcp
                else "/open_api/v1.3/smart_plus/campaign/create/"
            )
            if is_create:
                arguments = body["params"]["arguments"] if mcp else body
                with Session(database_engine) as db:
                    step = db.get(ExecutionStep, step_id)
                    assert step.phase == "REQUEST_ARMED" and step.request_body_digest
                creates.append(arguments)
                # 平台回读使用精确金额文本，不能把 SDK 创建 JSON 的 float 回显当作精度证据。
                effects.append(
                    {
                        **arguments,
                        "budget": str(arguments["budget"]),
                        "campaign_id": "late-original-id",
                    }
                )
                received.set()
                release.wait(30)
                value = {
                    "code": 0,
                    "data": {"campaign_id": "late-original-id"},
                    "request_id": "late-safe-request",
                }
                if mcp:
                    value = {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"content": [], "structuredContent": value},
                    }
                self.respond(
                    200,
                    json.dumps(value).encode(),
                    {"Content-Type": "application/json"},
                )
                return
            if mcp and operation == "smart_plus_campaign_get":
                reads.append(body["params"]["arguments"])
                self.respond(
                    200,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {
                                "content": [],
                                "structuredContent": self.read_envelope(),
                            },
                        }
                    ).encode(),
                    {"Content-Type": "application/json"},
                )
                return
            original = self.rfile
            try:
                self.rfile = io.BytesIO(raw)
                super().do_POST()
            finally:
                self.rfile = original

        def read_envelope(self):
            # The remote effect exists while the original create HTTP is still blocked.
            return {
                "code": 0,
                "data": {
                    "list": effects,
                    "page_info": {
                        "page": 1,
                        "page_size": 100,
                        "total_number": len(effects),
                        "total_page": 1,
                    },
                },
                "request_id": "read-safe-request",
            }

        def do_GET(self):
            assert self.path.startswith("/open_api/v1.3/smart_plus/campaign/get/")
            reads.append(self.path)
            self.respond(
                200,
                json.dumps(self.read_envelope()).encode(),
                {"Content-Type": "application/json"},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    # Restore the actual HTTP implementation hidden by the shared SDK fixture.
    from urllib3._request_methods import RequestMethods

    def local_sdk(pool, method, url, **kwargs):
        assert url.startswith("https://business-api.tiktok.com/")
        return RequestMethods.request(
            pool,
            method,
            endpoint + url.removeprefix("https://business-api.tiktok.com"),
            **kwargs,
        )

    monkeypatch.setattr(urllib3.PoolManager, "request", local_sdk)

    class LocalMcp(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            assert str(request.url) == load_mcp_protocol().endpoint
            request.url = httpx2.URL(endpoint + "/mcp")
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(transport, "_new_http_transport", LocalMcp)
    monkeypatch.setattr(execution, "_require_bounded_worker", worker_guard)
    prefix = f"read-prefork-{uuid4().hex}:"
    broker = os.environ["TEST_REDIS_URL"]
    app = Celery(prefix, broker=broker, set_as_current=False)
    app.conf.update(
        task_default_queue=prefix + "queue",
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"global_keyprefix": prefix},
        worker_hijack_root_logger=False,
    )

    @app.task(
        name=prefix + "create",
        shared=False,
        # 为 MCP 握手及逐请求数据库鉴权留出准备时间，再在远端阻塞时实际硬终止。
        time_limit=10,
        # 本例验证硬终止后的未知结果；软中断会在发送准备阶段提前结束任务。
        soft_time_limit=None,
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def create_task():
        database_engine.dispose(close=False)
        with Redis.from_url(broker) as client:
            result = execution.process_step(
                database_engine=database_engine,
                redis_client=client,
                context=context,
                step_id=step_id,
                revision=0,
            )
            client.set(prefix + "create-result", result, ex=120)

    @app.task(name=prefix + "read", shared=False, time_limit=40, soft_time_limit=35)
    def read_task(revision=0):
        database_engine.dispose(close=False)
        with Redis.from_url(broker) as client:
            result = reconciliation.process_reconciliation(
                database_engine=database_engine,
                redis_client=client,
                context=context,
                step_id=step_id,
                revision=revision,
            )
            client.set(prefix + "read-result", result.state, ex=120)

    try:
        with start_worker(
            app,
            pool="prefork",
            use_eventloop=False,
            concurrency=1,
            perform_ping_check=False,
            shutdown_timeout=15,
            queues=[prefix + "queue"],
            loglevel="ERROR",
        ) as worker:
            original_pid = worker.pool._pool._pool[0].pid
            create_task.delay()
            arrived = received.wait(10)
            with Session(database_engine) as db:
                observed = db.get(ExecutionStep, step_id)
                diagnostic = (
                    redis_client.get(prefix + "create-result"),
                    observed.status,
                    observed.error_code,
                    observed.phase,
                )
            assert arrived, diagnostic
            deadline = time.monotonic() + 12
            while (
                original_pid
                in [process.pid for process in tuple(worker.pool._pool._pool)]
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert original_pid not in [
                process.pid for process in tuple(worker.pool._pool._pool)
            ]
            assert len(creates) == 1 and not release.is_set()
            quota_keys = admission_keys(
                quota_scope(
                    channel=route.channel,
                    app_id=settings.TIKTOK_APP_ID,
                    verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
                ),
                "build.create_campaign",
                context.tenant_id,
                case["advertiser_id"],
            )
            assert redis_client.zcard(quota_keys[2]) == 1
            with Session(database_engine) as db, db.begin():
                step = db.get(ExecutionStep, step_id)
                frozen_body = dict(step.request_body)
                step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                assert expire_attempt(db, step=step) == "RECONCILE"
                alternate = TikTokConnection(
                    tenant_id=context.tenant_id, kind=route.channel, status="ACTIVE"
                )
                db.add(alternate)
                db.flush()
                db.add(
                    BCConnectionBinding(
                        tenant_id=context.tenant_id,
                        bc_id=route.bc_id,
                        connection_id=alternate.id,
                        kind=route.channel,
                    )
                )
                db.flush()
                default = db.get(BCDefaultRoute, (context.tenant_id, route.bc_id))
                default.connection_id = alternate.id
                db.add(default)
            read_task.delay()
            deadline = time.monotonic() + 20
            while (
                not redis_client.exists(prefix + "read-result")
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert redis_client.get(prefix + "read-result") in {
                b"SUCCEEDED",
                "SUCCEEDED",
            }, read_diagnostic(database_engine, step_id, reads)
            assert len(creates) == 1 and len(reads) == 1 and not release.is_set()
            release.set()  # A late response never authorizes another create.
            create_task.delay()
            with Session(database_engine) as db:
                step = db.get(ExecutionStep, step_id)
                assert (
                    step.request_body == frozen_body
                    and step.remote_id == "late-original-id"
                )
            assert len(creates) == 1
            with Session(database_engine) as db, db.begin():
                original_connection = db.get(TikTokConnection, route.connection_id)
                original_connection.status = "DISABLED"
                db.add(original_connection)
                step = db.get(ExecutionStep, step_id)
                step.dispatch_revision = 1
                db.add(step)
            redis_client.delete(prefix + "read-result")
            read_task.delay(1)
            deadline = time.monotonic() + 10
            while (
                not redis_client.exists(prefix + "read-result")
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert redis_client.get(prefix + "read-result") in {b"UNKNOWN", "UNKNOWN"}
            assert len(creates) == 1 and len(reads) == 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
