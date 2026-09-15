"""部署模板驱动真实 Redis 消费者：素材占满执行槽时广告仍能开始。"""

import configparser
import os
import shlex
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from celery import Celery


def deployment_workers():
    root = Path(__file__).resolve().parents[3]
    paths = [root / "deploy/staging-worker.service"]
    # 缺少独立模板时按真实旧部署启动唯一消费者，暴露素材阻塞广告的行为。
    independent = root / "deploy/staging-builds.service"
    if independent.exists():
        paths.append(independent)
    workers = []
    for path in paths:
        config = configparser.ConfigParser(interpolation=None)
        config.read(path)
        arguments = shlex.split(config["Service"]["ExecStart"])
        queues = arguments[arguments.index("-Q") + 1].split(",")
        concurrency = int(arguments[arguments.index("--concurrency") + 1])
        assert arguments[arguments.index("--pool") + 1] == "prefork"
        workers.append((queues, concurrency))
    return workers


@pytest.mark.parametrize("pool", ["threads", "prefork"])
def test_ready_ad_executes_while_all_material_slots_are_blocked(redis_client, pool):
    if pool == "prefork" and sys.platform != "linux":
        pytest.skip("真实 prefork 隔离证据在 Linux 验证，线程消费者只证明队列分配")
    workers = deployment_workers()
    material_slots = sum(count for queues, count in workers if "resources" in queues)
    prefix = f"worker-isolation-{uuid4().hex}:"
    broker = os.environ["TEST_REDIS_URL"]
    app = Celery(prefix, broker=broker, set_as_current=False)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"global_keyprefix": prefix},
        worker_hijack_root_logger=False,
        task_acks_late=True,
    )

    processes = []
    try:
        for index, (queues, count) in enumerate(workers):
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
                        ",".join(prefix + name for name in queues),
                        "--pool",
                        pool,
                        "--concurrency",
                        str(count),
                        "--hostname",
                        f"isolation-{index}@localhost",
                        "--loglevel",
                        "ERROR",
                        "--without-gossip",
                        "--without-mingle",
                    ],
                    env={**os.environ, "QUEUE_ISOLATION_PREFIX": prefix},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        for _ in workers:
            assert redis_client.blpop(prefix + "worker-ready", timeout=15)
        for _ in range(material_slots):
            app.send_task("test.isolation.material", queue=prefix + "resources")
        for _ in range(material_slots):
            assert redis_client.blpop(prefix + "started", timeout=10)
        app.send_task("test.isolation.ad", queue=prefix + "builds")
        assert redis_client.blpop(prefix + "ad-complete", timeout=3), (
            "已就绪广告仍被占满的素材执行槽阻塞"
        )
    finally:
        for _ in range(material_slots):
            redis_client.lpush(prefix + "release", "release")
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        app.close()
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
