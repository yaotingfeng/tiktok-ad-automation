"""隔离子进程中的真实 Redis worker；不导入业务任务或调用外部服务。"""

import os
import time

from celery import Celery
from celery.signals import worker_ready
from redis import Redis

broker = os.environ["TEST_REDIS_URL"]
prefix = os.environ["QUEUE_ISOLATION_PREFIX"]
app = Celery("queue-isolation", broker=broker)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    broker_transport_options={"global_keyprefix": prefix},
)


@worker_ready.connect
def ready(**_kwargs):
    with Redis.from_url(broker) as redis:
        redis.lpush(prefix + "worker-ready", "ready")


@app.task(name="test.isolation.material")
def blocked_material():
    with Redis.from_url(broker) as redis:
        redis.lpush(prefix + "started", "material")
        redis.blpop(prefix + "release", timeout=60)


@app.task(name="test.isolation.ad")
def ready_ad():
    with Redis.from_url(broker) as redis:
        redis.lpush(prefix + "ad-complete", "ad")


@app.task(name="test.isolation.material_result")
def material_result():
    with Redis.from_url(broker) as redis:
        redis.lpush(prefix + "result-complete", redis.llen(prefix + "ad-complete"))


@app.task(name="test.isolation.queue_probe")
def queue_probe(label, delay=0.0):
    """独立队列积压探针：只记录真实 Redis worker 的执行时序。"""
    if delay:
        time.sleep(float(delay))
    with Redis.from_url(broker) as redis:
        redis.lpush(prefix + f"probe:{label}", "done")
