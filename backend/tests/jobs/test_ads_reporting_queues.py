import os
import subprocess
import sys
from pathlib import Path

import pytest
from celery import Celery

from app.core.config import settings
from app.jobs.celery_app import celery_app
from app.jobs.queued_dispatches import _QUEUES
from app.jobs.tasks import dispatch_queue


def test_ads_reporting_tasks_have_dedicated_queues_and_standard_beat_gate():
    celery_app.loader.import_default_modules()
    assert dispatch_queue("ads.sync_step") == "ads-directory"
    assert dispatch_queue("ads.publish_step") == "ads-publish"
    assert dispatch_queue("ads.targeted_step") == "ad-management"
    assert celery_app.conf.task_routes["ads.targeted_step"]["queue"] == "ad-management"
    assert "ads-publish" in _QUEUES
    assert dispatch_queue("reporting.job_step") == "ads-reporting"
    assert celery_app.conf.task_routes["reporting.plan_due"]["queue"] == "control"
    assert "scan-ads-reporting-due" in celery_app.conf.beat_schedule
    assert "ads-directory" in _QUEUES
    assert "ads-reporting" in _QUEUES

    previous = settings.ADS_SYNC_ENABLED
    settings.ADS_SYNC_ENABLED = False
    try:
        from app.modules.reporting.job_tasks import plan_due_runs

        assert plan_due_runs(database_engine=None, redis_client=None) == 0
    finally:
        settings.ADS_SYNC_ENABLED = previous


def test_worker_templates_keep_one_slot_and_shared_switch_injection():
    root = Path(__file__).resolve().parents[3]
    compose = (root / "compose.yml").read_text()
    production = (root / "compose.production.yml").read_text()
    directory_service = (root / "deploy/staging-ads-directory.service").read_text()
    reporting_service = (root / "deploy/staging-ads-reporting.service").read_text()
    assert "ADS_SYNC_ENABLED" in compose
    assert '-Q, "ads-directory,ads-publish"' in compose
    assert "-Q, ads-reporting" in compose
    assert '-Q, "ads-directory,ads-publish"' in production
    assert "-Q, ads-reporting" in production
    assert "--concurrency" in compose
    assert "--concurrency" in production
    assert "--concurrency 1" in directory_service
    assert "--concurrency 1" in reporting_service
    import yaml

    workloads = list(
        yaml.safe_load_all((root / "deploy/k8s/tk-ada-workloads.yaml").read_text())
    )
    consumers = []
    for deployment in workloads:
        if deployment.get("kind") != "Deployment":
            continue
        command = deployment["spec"]["template"]["spec"]["containers"][0].get(
            "command", []
        )
        if "-Q" in command and "ads-publish" in command[command.index("-Q") + 1].split(
            ","
        ):
            consumers.append(deployment["metadata"]["name"])
            assert command[command.index("--concurrency") + 1] == "1"
    assert consumers == ["tiktok-ad-automation-worker-ads-directory"]


@pytest.mark.parametrize("target", ["ads-reporting", "ads-publish"])
def test_real_redis_workers_keep_reporting_live_during_directory_backlog(
    redis_client, target
):
    """真实 Redis worker：目录积压不挤占报表槽，也不饿死同槽发布队列。"""

    from uuid import uuid4

    root = Path(__file__).resolve().parents[3]
    prefix = f"queue-share-{uuid4().hex}:"
    broker = os.environ["TEST_REDIS_URL"]
    producer = Celery(
        f"a8-producer-{prefix}",
        broker=broker,
        set_as_current=False,
    )
    producer.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        task_create_missing_queues=True,
        broker_transport_options={"global_keyprefix": prefix},
    )
    processes = []
    try:
        for queue in (
            ("ads-directory", "ads-reporting")
            if target == "ads-reporting"
            else ("ads-directory," + prefix + "ads-publish",)
        ):
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
                        f"a8-{queue}@localhost",
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
        for _ in range(50):
            producer.send_task(
                "test.isolation.queue_probe",
                args=["directory", 0.03],
                queue=prefix + "ads-directory",
            )
        producer.send_task(
            "test.isolation.queue_probe",
            args=["reporting", 0],
            queue=prefix + target,
        )
        assert redis_client.blpop(prefix + "probe:reporting", timeout=3)
        if target == "ads-publish":
            assert redis_client.llen(prefix + "probe:directory") < 50
        # 确认目录工作也真实执行，避免只验证空队列的快速返回。
        assert redis_client.blpop(prefix + "probe:directory", timeout=5)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)
        producer.close()
