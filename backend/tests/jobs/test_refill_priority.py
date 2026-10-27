"""真实 Redis 传输：纯补位让出 flush，原身份及所有优先级补投识别保持。"""

import os
from uuid import uuid4

import pytest
from celery import Celery
from sqlmodel import Session

from app.core.config import settings
from app.jobs import outbox, queued_dispatches
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit
from app.modules.reporting import job_tasks  # noqa: F401
from tests.jobs.test_outbox import context as context
from tests.jobs.test_outbox import outbox_db as outbox_db
from tests.jobs.test_queued_dispatches import request_redelivery


@pytest.fixture
def real_broker(redis_client, monkeypatch):
    prefix = f"refill-priority:{uuid4().hex}:"
    app = Celery("refill-priority", broker=os.environ["TEST_REDIS_URL"])
    app.conf.update(
        task_queues=celery_app.conf.task_queues,
        task_routes=celery_app.conf.task_routes,
        task_default_queue="control",
        task_create_missing_queues=False,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        broker_transport_options={
            **celery_app.conf.broker_transport_options,
            "global_keyprefix": prefix,
        },
    )
    monkeypatch.setattr(outbox, "celery_app", app)
    monkeypatch.setattr(queued_dispatches, "celery_app", app)
    monkeypatch.setattr(queued_dispatches, "_cache", None)
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    yield app, redis_client, prefix
    app.close()
    keys = list(redis_client.scan_iter(prefix + "*"))
    if keys:
        redis_client.delete(*keys)


def test_real_publisher_deprioritizes_refill_and_flush_overtakes_it(
    outbox_db, context, real_broker
):
    app, redis, prefix = real_broker
    with Session(outbox_db) as session, session.begin():
        identity = enqueue_after_commit(
            session,
            context=context,
            task_name="reporting.plan_due",
            task_key="refill",
            payload={"dispatch_only": True},
        )
    assert outbox.flush_dispatch() == 1
    assert redis.llen(prefix + "control\x06\x163") == 1
    assert redis.llen(prefix + "control") == 0
    envelope = {
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "payload": {"dispatch_only": True},
    }
    assert queued_dispatches.queued_dispatches().contains(
        queue="control",
        name="reporting.plan_due",
        task_id=str(identity),
        kwargs=envelope,
    )
    request_redelivery(outbox_db, identity)
    assert outbox.flush_dispatch() == 1
    assert redis.llen(prefix + "control\x06\x163") == 1
    app.send_task("jobs.flush_dispatch", queue="control")
    with app.connection_for_read() as connection:
        with connection.channel() as channel:
            assert tuple(channel.priority_steps) == (0, 3, 6, 9)
            assert channel._get("control")["headers"]["task"] == "jobs.flush_dispatch"
            assert channel._get("control")["headers"]["id"] == str(identity)


@pytest.mark.parametrize(
    "name,payload",
    [("reporting.plan_due", {}), ("jobs.probe", {"dispatch_only": True})],
)
def test_other_dispatch_keeps_default_priority(
    outbox_db, context, real_broker, name, payload
):
    _, redis, prefix = real_broker
    with Session(outbox_db) as session, session.begin():
        enqueue_after_commit(
            session,
            context=context,
            task_name=name,
            task_key="ordinary",
            payload=payload,
        )
    assert outbox.flush_dispatch() == 1
    assert redis.llen(prefix + "control") == 1
    assert redis.llen(prefix + "control\x06\x163") == 0


@pytest.mark.parametrize("priority", [0, 3, 6, 9])
def test_queued_snapshot_covers_every_default_transport_priority(
    real_broker, context, priority
):
    app, _, _ = real_broker
    identity = str(uuid4())
    kwargs = {
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "payload": {},
    }
    app.send_task(
        "jobs.probe",
        kwargs=kwargs,
        task_id=identity,
        queue="control",
        priority=priority,
    )
    assert queued_dispatches.queued_dispatches().contains(
        queue="control", name="jobs.probe", task_id=identity, kwargs=kwargs
    )
