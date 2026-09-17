"""真实 PG/Redis：补投不能放大已排队消息，也不能吞掉真正丢失的消息。"""

import base64
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.config import settings
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import flush_dispatch
from tests.jobs.test_outbox import context as context
from tests.jobs.test_outbox import enqueue
from tests.jobs.test_outbox import outbox_db as outbox_db


def broker_message(name, task_id, kwargs):
    return json.dumps(
        {
            "headers": {"id": task_id, "task": name},
            "properties": {"body_encoding": "base64"},
            "body": base64.b64encode(json.dumps([[], kwargs, {}]).encode()).decode(),
        }
    )


@pytest.fixture
def broker(redis_client, monkeypatch):
    prefix = f"queued-dispatch-{uuid4().hex}:"
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setitem(
        celery_app.conf, "broker_transport_options", {"global_keyprefix": prefix}
    )

    def publish(name, *, kwargs, task_id, queue):
        redis_client.lpush(prefix + queue, broker_message(name, task_id, kwargs))

    monkeypatch.setattr(celery_app, "send_task", publish)
    yield redis_client, prefix
    owned = list(redis_client.scan_iter(prefix + "*"))
    if owned:
        redis_client.delete(*owned)


def request_redelivery(engine, identity):
    with Session(engine) as db, db.begin():
        row = db.get(PendingDispatch, identity)
        row.published_at = None
        row.available_at = datetime.now(UTC)


def test_repeated_watchdog_keeps_one_queued_message(outbox_db, context, broker):
    redis, prefix = broker
    with Session(outbox_db) as db, db.begin():
        identity = enqueue(db, context, item_id="one")
    assert flush_dispatch() == 1
    for _ in range(3):
        request_redelivery(outbox_db, identity)
        flush_dispatch()
    assert redis.llen(prefix + "control") == 1
    with Session(outbox_db) as db:
        assert db.get(PendingDispatch, identity).published_at is not None


def test_missing_message_is_republished_with_same_identity(outbox_db, context, broker):
    from app.jobs.queued_dispatches import queued_dispatches

    redis, prefix = broker
    with Session(outbox_db) as db, db.begin():
        identity = enqueue(db, context, item_id="lost")
    assert flush_dispatch() == 1
    assert queued_dispatches().contains(
        queue="control",
        name="jobs.probe",
        task_id=str(identity),
        kwargs={
            "tenant_id": str(context.tenant_id),
            "actor_id": str(context.actor_id),
            "payload": {"item_id": "lost"},
        },
    )
    original = redis.rpop(prefix + "control")
    request_redelivery(outbox_db, identity)
    assert flush_dispatch() == 1
    assert redis.llen(prefix + "control") == 1
    assert redis.rpop(prefix + "control") == original


@pytest.mark.parametrize("different", ["tenant", "actor", "payload", "task", "queue"])
def test_wrong_envelope_never_suppresses_original(
    outbox_db, context, broker, different
):
    redis, prefix = broker
    with Session(outbox_db) as db, db.begin():
        identity = enqueue(db, context, item_id="correct")
    kwargs = {
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id),
        "payload": {"item_id": "correct"},
    }
    name, queue = "jobs.probe", "control"
    if different in {"tenant", "actor"}:
        kwargs[different + "_id"] = str(uuid4())
    elif different == "payload":
        kwargs["payload"] = {"item_id": "wrong"}
    elif different == "task":
        name = "jobs.wrong"
    else:
        queue = "resources"
    redis.lpush(prefix + queue, broker_message(name, str(identity), kwargs))
    assert flush_dispatch() == 1
    assert redis.llen(prefix + "control") == (1 if different == "queue" else 2)


def test_malformed_messages_do_not_block_delivery(outbox_db, context, broker):
    redis, prefix = broker
    redis.lpush(prefix + "control", "invalid-json", json.dumps({"body": "?"}))
    with Session(outbox_db) as db, db.begin():
        enqueue(db, context)
    assert flush_dispatch() == 1
    assert redis.llen(prefix + "control") == 3


@pytest.mark.parametrize("properties", [None, [], "invalid"])
def test_malformed_properties_are_ignored(properties):
    from app.jobs.queued_dispatches import _message_identity

    message = json.loads(broker_message("jobs.probe", "id", {}))
    message["properties"] = properties
    assert _message_identity(json.dumps(message)) is None


@pytest.mark.parametrize("decode", [True, False])
def test_stopped_queue_move_preserves_bytes_duplicates_and_order(broker, decode):
    from redis import Redis

    from app.jobs.result_queue_migration import move_result_messages

    _, prefix = broker
    redis = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=decode)
    first = broker_message("materials.verify_target", "first", {})
    last = broker_message("materials.prepare_cover", "last", {})
    upload = broker_message("materials.prepare_target", "upload", {})
    redis.lpush(prefix + "resources", first, upload, first, last, "invalid")
    result = move_result_messages(redis, prefix=prefix)
    assert result == {"moved": 3, "messages_before": 5, "messages_after": 5}

    def value(raw):
        return raw if decode else raw.encode()

    assert redis.lrange(prefix + "resources", 0, -1) == [
        value("invalid"),
        value(upload),
    ]
    assert redis.lrange(prefix + "resource-results", 0, -1) == [
        value(last),
        value(first),
        value(first),
    ]
    assert move_result_messages(redis, prefix=prefix)["moved"] == 0
    redis.close()
