"""Linux CI exercises the actual prefork hard deadline against a stalled SDK."""

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from app.integrations.tiktok import sdk as sdk_scope
from app.jobs.admission import admission_keys
from app.modules.builds import execution
from app.modules.builds.execution import (
    _require_bounded_worker as production_worker_guard,
)
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.execution_state import expire_attempt
from app.modules.builds.sdk_requests import PORTFOLIO_ENDPOINT
from tests.modules.builds.test_execution import executable as executable


@pytest.mark.skipif(
    sys.platform != "linux", reason="prefork hard-deadline evidence runs on Linux CI"
)
def test_hard_kill_cannot_replay_an_armed_official_create(
    executable, redis_client, monkeypatch
):
    db, context, ids = executable
    step_id = ids["CTA"][0]
    received, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802 - standard HTTP handler override
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with Session(db) as session:
                step = session.get(ExecutionStep, step_id)
                requests.append((body, step.phase, step.request_body_digest))
            received.set()
            release.wait(20)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_client = sdk_scope.official_client

    @contextmanager
    def local_sdk(*, access_token=None):
        with original_client(access_token=access_token) as client:
            client.configuration.host = f"http://127.0.0.1:{server.server_port}"
            yield client

    monkeypatch.setattr(sdk_scope, "official_client", local_sdk)
    monkeypatch.setattr(execution, "_require_bounded_worker", production_worker_guard)
    prefix = f"prefork-test-{uuid4().hex}:"
    queue_name = prefix + "builds"
    broker_url = os.environ["TEST_REDIS_URL"]
    test_app = Celery(prefix, broker=broker_url, set_as_current=False)
    test_app.conf.update(
        task_default_queue=queue_name,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"global_keyprefix": prefix},
        worker_hijack_root_logger=False,
    )

    @test_app.task(
        name=prefix + "create",
        shared=False,
        time_limit=3,
        soft_time_limit=1,
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def stalled_create():
        db.dispose(close=False)
        with Redis.from_url(broker_url) as client:
            execution.process_step(
                database_engine=db,
                redis_client=client,
                context=context,
                step_id=step_id,
                revision=0,
            )
            client.set(prefix + "returned", "yes", ex=120)

    keys = admission_keys(
        settings.TIKTOK_APP_ID, PORTFOLIO_ENDPOINT, context.tenant_id, "account-A"
    )
    try:
        with start_worker(
            test_app,
            pool="prefork",
            concurrency=1,
            perform_ping_check=False,
            shutdown_timeout=15,
            queues=[queue_name],
            loglevel="ERROR",
        ) as worker:
            original_pid = worker.pool._pool._pool[0].pid
            started = time.monotonic()
            stalled_create.delay()
            assert received.wait(10), "official SDK did not reach the local transport"
            assert requests[0][1] == "REQUEST_ARMED" and requests[0][2]
            deadline = time.monotonic() + 12
            replacement = []
            while time.monotonic() < deadline:
                replacement = [p.pid for p in tuple(worker.pool._pool._pool)]
                if replacement and original_pid not in replacement:
                    break
                time.sleep(0.05)
            assert replacement and original_pid not in replacement, (
                "hard deadline did not replace the blocked child"
            )
            assert time.monotonic() - started < 15
            assert not redis_client.exists(prefix + "returned")
            # The actual SDK thread was killed; its admission is still fenced by
            # a lease longer than the whole process deadline, never released early.
            assert redis_client.zcard(keys[2]) == 1
            with Session(db) as session, session.begin():
                step = session.get(ExecutionStep, step_id)
                assert step.request_body is not None and step.remote_id is None
                step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                assert expire_attempt(session, step=step) == "RECONCILE"
            stalled_create.delay()
            deadline = time.monotonic() + 8
            while (
                not redis_client.exists(prefix + "returned")
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert redis_client.exists(prefix + "returned")
            assert len(requests) == 1
            with Session(db) as session:
                assert session.get(ExecutionStep, step_id).status == "UNKNOWN"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        test_app.close()
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
        redis_client.delete(*keys)
