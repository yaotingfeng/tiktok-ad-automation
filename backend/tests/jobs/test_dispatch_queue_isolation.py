"""真实Redis消费者：control阻塞时，持久消息投递仍有现有builds槽可用。"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from celery import Celery

from app.jobs.celery_app import celery_app


def test_dispatch_remains_deliverable_while_control_is_blocked(redis_client):
    route = celery_app.conf.task_routes["jobs.flush_dispatch"]["queue"]
    tick = celery_app.conf.beat_schedule["flush-dispatch"]
    assert route == tick["options"]["queue"] == "builds"
    assert tick["schedule"] == 1.0
    assert tick["options"]["priority"] == 0
    prefix = f"dispatch-isolation-{uuid4().hex}:"
    producer = Celery("dispatch-isolation", broker=os.environ["TEST_REDIS_URL"])
    producer.conf.update(
        task_ignore_result=True, broker_transport_options={"global_keyprefix": prefix}
    )
    processes = []
    root = Path(__file__).resolve().parents[3]
    try:
        for queue in ("control", route):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "tests.jobs.queue_isolation_worker:app",
                        "worker",
                        "-Q",
                        prefix + queue,
                        "--pool",
                        "threads",
                        "--concurrency",
                        "1",
                        "--hostname",
                        f"{prefix}{queue}@localhost",
                        "--loglevel",
                        "ERROR",
                        "--without-gossip",
                        "--without-mingle",
                    ],
                    cwd=root / "backend",
                    env={**os.environ, "QUEUE_ISOLATION_PREFIX": prefix},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        for _ in processes:
            assert redis_client.blpop(prefix + "worker-ready", timeout=15)
        producer.send_task("test.isolation.material", queue=prefix + "control")
        assert redis_client.blpop(prefix + "started", timeout=5)
        # 使用真实应用的投递路由；测试任务只记录到达，不调用外部平台。
        for _ in range(100):
            producer.send_task(
                "test.isolation.queue_probe",
                args=["build", 0.1],
                queue=prefix + route,
                priority=3,
            )
        producer.send_task("test.isolation.ad", queue=prefix + route, priority=0)
        assert redis_client.blpop(prefix + "ad-complete", timeout=5)
        assert not redis_client.llen(prefix + "release")
    finally:
        redis_client.lpush(prefix + "release", "done")
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        keys = list(redis_client.scan_iter(match=prefix + "*"))
        if keys:
            redis_client.delete(*keys)
        producer.close()
