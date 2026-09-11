"""Linux 双真实 prefork worker：上游仍拉取时本地超时不释放原件。

本机 macOS skip 不是验收通过。只把 HTTP/S3 边界指向本地服务；生产
task guard、gateway、PG claim 与 Redis 准入全部真实执行。
"""

# ruff: noqa: F401,F811 -- fixture composition, isolated real database

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
from sqlmodel import Session, select

from app.core.config import settings
from app.integrations.tiktok.admission import quota_scope
from app.integrations.tiktok.mcp import transport
from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
from app.jobs.admission import admission_keys
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TikTokConnection
from app.modules.materials import object_validation, tasks
from app.modules.materials.cleanup import run_cleanup
from app.modules.materials.ingest_models import ObjectCleanup, TemporaryMaterialObject
from app.modules.materials.ingest_schemas import IngestIdentity
from app.modules.materials.ingest_transport import cancel_file
from tests.modules.materials import test_url_ingest
from tests.modules.materials.test_unknown_original_fences import (
    ForbidDelete,
    app_config,
    assert_reserved,
    channel_source_env,
    charged_original,
    expire_local_clocks,
    gateway_case,
    gateway_wire,
    info,
    operation,
    policy,
    source_env,
    synthetic_contract,
    url_env,
)
from tests.modules.strategies.test_concurrency import isolated_strategy_database


@pytest.fixture
def database_engine(isolated_strategy_database, monkeypatch):
    database_engine = isolated_strategy_database[0]
    monkeypatch.setattr(test_url_ingest, "engine", database_engine)
    monkeypatch.setattr(tasks, "engine", database_engine)
    return database_engine


def wait_for(predicate, seconds=15):
    until = time.monotonic() + seconds
    while not predicate() and time.monotonic() < until:
        time.sleep(0.05)
    assert predicate(), "有界真实 worker 观察未到达"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="actual two-worker prefork validation requires Linux",
)
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "lost_process", [False, True], ids=["late-live-receipt", "hard-killed"]
)
def test_unknown_remote_pull_survives_two_workers_and_late_result(
    charged_original,
    gateway_case,
    gateway_wire,
    synthetic_contract,
    database_engine,
    redis_client,
    monkeypatch,
    lost_process,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    context, route, advertiser = gateway_case
    received, release = threading.Event(), threading.Event()
    creates, reads, tokens = [], [], []
    original_handler = gateway_wire["wire"].server.RequestHandlerClass

    class Handler(original_handler):
        def envelope(self, *, search):
            op = operation(env)
            data = info(
                file_name=op.remote_response["remote_name"], advertiser_id=advertiser
            )
            if search:
                data["page_info"] = {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                }
            return {"code": 0, "data": data, "request_id": "synthetic-read"}

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            mcp = route.channel == "OFFICIAL_MCP"
            body = json.loads(raw) if mcp else None
            name = body.get("params", {}).get("name") if mcp else self.path
            if name == (
                "file_video_ad_upload"
                if mcp
                else "/open_api/v1.3/file/video/ad/upload/"
            ):
                op = operation(env)
                assert op.remote_response["send_armed"] is True
                with Session(database_engine) as db:
                    assert_reserved(db, env, op.id)
                creates.append(op.id)
                tokens.append(
                    self.headers.get("Authorization")
                    if mcp
                    else self.headers.get("Access-Token")
                )
                received.set()
                # 仅本地服务阻塞，真实远端效果已存在但原 HTTP 尚未返回。
                release.wait(40)
                value = {
                    "code": 0,
                    "data": {
                        "video_id": "actual-source-vid",
                        "material_id": "actual-source-mid",
                    },
                }
                if mcp:
                    value = {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"content": [], "structuredContent": value},
                    }
                else:
                    value["data"] = [value["data"]]
                self.respond(
                    200,
                    json.dumps(value).encode(),
                    {"Content-Type": "application/json"},
                )
                return
            if mcp and name in {"file_video_ad_search", "file_video_ad_info_get"}:
                reads.append(name)
                tokens.append(self.headers.get("Authorization"))
                value = {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [],
                        "structuredContent": self.envelope(
                            search=name == "file_video_ad_search"
                        ),
                    },
                }
                self.respond(
                    200,
                    json.dumps(value).encode(),
                    {"Content-Type": "application/json"},
                )
                return
            original = self.rfile
            try:
                self.rfile = io.BytesIO(raw)
                super().do_POST()
            finally:
                self.rfile = original

        def do_GET(self):
            if route.channel == "OFFICIAL_MCP":
                return super().do_GET()
            assert self.path.startswith("/open_api/v1.3/file/video/ad/")
            reads.append(self.path)
            tokens.append(self.headers.get("Access-Token"))
            self.respond(
                200,
                json.dumps(self.envelope(search="/search/" in self.path)).encode(),
                {"Content-Type": "application/json"},
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    from urllib3._request_methods import RequestMethods

    def local_sdk(pool, method, url, **kwargs):
        assert url.startswith("https://business-api.tiktok.com/")
        return RequestMethods.request(
            pool,
            method,
            endpoint + url.removeprefix("https://business-api.tiktok.com"),
            **kwargs,
        )

    class LocalMcp(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            assert str(request.url) == load_mcp_protocol().endpoint
            request.url = httpx2.URL(endpoint + "/mcp")
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(urllib3.PoolManager, "request", local_sdk)
    monkeypatch.setattr(transport, "_new_http_transport", LocalMcp)
    monkeypatch.setattr(object_validation, "make_object_s3", lambda obj: env["s3"])
    prefix = f"source-prefork-{uuid4().hex}:"
    broker = os.environ["TEST_REDIS_URL"]
    app = Celery(prefix, broker=broker, set_as_current=False)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        task_acks_on_failure_or_timeout=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"global_keyprefix": prefix},
        worker_hijack_root_logger=False,
    )
    original_upload = tasks.upload_original.run.__func__
    original_verify = tasks.verify_original.run.__func__
    payload = {
        "material_id": str(env["material_id"]),
        "object_id": str(env["object_id"]),
        "generation": env["generation"],
    }

    @app.task(
        name=prefix + "upload",
        bind=True,
        shared=False,
        time_limit=3 if lost_process else 35,
        soft_time_limit=None,
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def upload(self):
        database_engine.dispose(close=False)
        with Redis.from_url(broker) as client:
            client.set(prefix + "upload-pid", os.getpid(), ex=120)
        original_upload(
            self,
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload=payload,
        )
        with Redis.from_url(broker) as client:
            client.incr(prefix + "upload-returned")

    @app.task(name=prefix + "maintenance", bind=True, shared=False, time_limit=30)
    def maintenance(self, action):
        database_engine.dispose(close=False)
        with Redis.from_url(broker) as client:
            client.set(prefix + "maintenance-pid", os.getpid(), ex=120)
        if action == "verify":
            original_verify(
                self,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                payload={**payload, "operation_id": str(operation(env).id)},
            )
        elif action == "duplicate":
            original_upload(
                self,
                tenant_id=str(context.tenant_id),
                actor_id=str(context.actor_id),
                payload=payload,
            )
        else:
            tasks.require_bounded_worker(self, hard_limit=30)
            with Session(database_engine) as db:
                obj = db.get(TemporaryMaterialObject, env["object_id"])
                identity = IngestIdentity(
                    generation=obj.generation,
                    upload_id=obj.s3_upload_id,
                    operation_revision=obj.revision,
                )
            cancel_file(
                database_engine=database_engine,
                context=context,
                session_id=env["session_id"],
                material_id=env["material_id"],
                identity=identity,
            )
            with Session(database_engine) as db:
                cleanup = db.exec(
                    select(ObjectCleanup).where(
                        ObjectCleanup.material_id == env["material_id"]
                    )
                ).one()
                cleanup_id = cleanup.id
            run_cleanup(
                database_engine=database_engine,
                cleanup_id=cleanup_id,
                s3=ForbidDelete(),
            )
        with Redis.from_url(broker) as client:
            client.incr(prefix + action)

    def dispatch(action):
        before = int(redis_client.get(prefix + action) or 0)
        maintenance.apply_async(args=[action], queue=prefix + "second")
        wait_for(lambda: int(redis_client.get(prefix + action) or 0) > before)

    try:
        with (
            start_worker(
                app,
                pool="prefork",
                concurrency=1,
                perform_ping_check=False,
                shutdown_timeout=15,
                queues=[prefix + "first"],
                loglevel="ERROR",
            ) as first,
            start_worker(
                app,
                pool="prefork",
                concurrency=1,
                perform_ping_check=False,
                shutdown_timeout=15,
                queues=[prefix + "second"],
                loglevel="ERROR",
            ) as second,
        ):
            old_pid = first.pool._pool._pool[0].pid
            assert old_pid != second.pool._pool._pool[0].pid
            upload.apply_async(queue=prefix + "first")
            assert received.wait(10)
            assert int(redis_client.get(prefix + "upload-pid")) == old_pid
            if lost_process:
                wait_for(
                    lambda: (
                        old_pid not in [p.pid for p in tuple(first.pool._pool._pool)]
                    )
                )
                assert not redis_client.exists(prefix + "upload-returned")
            dispatch("duplicate")
            assert int(redis_client.get(prefix + "maintenance-pid")) != old_pid
            op = operation(env)
            expire_local_clocks(database_engine, env, op.id)
            scope = quota_scope(
                channel=route.channel,
                app_id=settings.TIKTOK_APP_ID,
                verified_service_scope=settings.MCP_SERVICE_QUOTA_SCOPE,
            )
            keys = admission_keys(
                scope, "materials.upload_video_url", context.tenant_id, advertiser
            )
            # 只使本测试tenant的确切租约到期，不能删除其他请求的共享quota。
            leases = redis_client.zrange(keys[4], 0, -1)
            assert leases
            for key in keys[2:]:
                for lease in leases:
                    if redis_client.zscore(key, lease) is not None:
                        redis_client.zadd(key, {lease: 0}, xx=True)
            dispatch("cleanup")
            with Session(database_engine) as db:
                assert_reserved(db, env, op.id)
            assert len(creates) == 1 and not release.is_set()
            with Session(database_engine) as db, db.begin():
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
                db.get(
                    BCDefaultRoute, (context.tenant_id, route.bc_id)
                ).connection_id = alternate.id
            dispatch("verify")  # 完整集合唯一匹配只能保留身份，仍未释放用途。
            assert operation(env).remote_response["video_id"] == "actual-source-vid"
            with Session(database_engine) as db:
                assert_reserved(db, env, op.id)
            dispatch("verify")  # 同原连接按实际ID精确回读，才可完成用途。
            assert operation(env).status == "succeeded"
            assert len(creates) == 1 and len(reads) == 2
            assert len(set(tokens)) == 1
            release.set()
            if not lost_process:
                wait_for(lambda: redis_client.exists(prefix + "upload-returned"))
            dispatch("duplicate")
            assert operation(env).status == "succeeded" and len(creates) == 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
