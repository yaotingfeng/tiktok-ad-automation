"""真实部署队列绑定与Redis消费：闲置准备槽帮助核验，双积压仍公平轮询。"""

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from celery import Celery
from kombu import Connection, Consumer, Producer, Queue

from app.jobs.celery_app import celery_app
from tests.jobs.test_worker_queue_isolation import deployment_workers


@pytest.mark.parametrize("pool", ["threads", "prefork"])
def test_idle_prepare_worker_consumes_waiting_material_results(redis_client, pool):
    if pool == "prefork" and sys.platform != "linux":
        pytest.skip("Linux验证真实prefork；本地线程只验证实际消费绑定")
    queues, slots = next(row for row in deployment_workers() if "resources" in row[0])
    prefix = f"staging-help-{uuid4().hex}:"
    app = Celery(prefix, broker=os.environ["TEST_REDIS_URL"], set_as_current=False)
    app.conf.broker_transport_options = {"global_keyprefix": prefix}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "tests.jobs.queue_isolation_worker:app",
            "worker",
            "-Q",
            ",".join(prefix + name for name in queues),
            "--pool",
            pool,
            "--concurrency",
            str(slots),
            "--hostname",
            "staging-help@localhost",
            "--loglevel",
            "ERROR",
            "--without-gossip",
            "--without-mingle",
        ],
        env={**os.environ, "QUEUE_ISOLATION_PREFIX": prefix},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert redis_client.blpop(prefix + "worker-ready", timeout=15)
        # 专用结果槽不参与本测试；仅依靠空闲的准备池完成结果读取。
        app.send_task(
            "test.isolation.material_result", queue=prefix + "resource-results"
        )
        assert redis_client.blpop(prefix + "result-complete", timeout=3), (
            "准备池空闲仍不能接走已等待的结果消息"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        app.close()
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)


def test_shared_prepare_binding_round_robins_two_backlogged_queues(redis_client):
    names, _ = next(row for row in deployment_workers() if "resources" in row[0])
    prefix = f"staging-fair-{uuid4().hex}:"
    observed = []
    options = dict(celery_app.conf.broker_transport_options)
    options["global_keyprefix"] = prefix
    try:
        with Connection(
            os.environ["TEST_REDIS_URL"], transport_options=options
        ) as broker:
            channel = broker.channel()
            # 两队列均预先堆满，直接消费部署绑定，避免靠消息恰好到达的时序证明公平。
            for name in ("resources", "resource-results"):
                queue = Queue(prefix + name, channel=channel)
                queue.declare()
                for index in range(20):
                    Producer(channel).publish(
                        {"queue": name, "index": index},
                        exchange="",
                        routing_key=prefix + name,
                        serializer="json",
                    )

            def received(body, message):
                observed.append(body["queue"])
                message.ack()

            with Consumer(
                channel,
                queues=[Queue(prefix + name) for name in names],
                callbacks=[received],
                accept=["json"],
            ):
                for _ in range(20):
                    broker.drain_events(timeout=3)
            # 任意连续两次读取均给双方机会，不让视频核验或准备因另一队列满而饿死。
            for start in range(0, 20, 2):
                assert set(observed[start : start + 2]) == {
                    "resources",
                    "resource-results",
                }
    finally:
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
