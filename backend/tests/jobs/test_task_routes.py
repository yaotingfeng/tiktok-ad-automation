"""真实 Redis/Celery 序列化验证旧队列交接；不执行远端业务。"""

import base64
import json
import os
from uuid import uuid4

import pytest
from celery import Celery
from celery.exceptions import Reject, Retry
from kombu.messaging import Producer

from app.jobs.celery_app import celery_app
from app.jobs.task_routes import ensure_dispatch_queue


@pytest.mark.parametrize("delivered", ["resources", "resource-results"])
@pytest.mark.parametrize("publish_failure", [False, True])
def test_cover_handoff_keeps_exact_task_identity_and_payload(
    redis_client, delivered, publish_failure, monkeypatch
):
    celery_app.loader.import_default_modules()
    prefix = f"cover-handoff-{uuid4().hex}:"
    app = Celery(prefix, broker=os.environ["TEST_REDIS_URL"], set_as_current=False)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        broker_transport_options={"global_keyprefix": prefix},
    )

    @app.task(bind=True, name="materials.prepare_cover", shared=False)
    def task(self, **_kwargs):
        ensure_dispatch_queue(self)

    identity = str(uuid4())
    kwargs = {
        "tenant_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "payload": {"job_id": str(uuid4()), "revision": 7},
    }
    try:
        for _ in range(2):
            task.push_request(
                id=identity,
                args=[],
                kwargs=kwargs,
                delivery_info={"routing_key": delivered, "exchange": ""},
                called_directly=False,
                is_eager=False,
                retries=0,
            )
            try:
                if delivered == "resources":
                    ensure_dispatch_queue(task)
                else:
                    if publish_failure:
                        # Broker边界失败不能吞掉原任务后继续业务；恢复投递仍沿用原ID。
                        def unavailable(*_args, **_kwargs):
                            raise OSError("test broker unavailable")

                        with monkeypatch.context() as patch:
                            patch.setattr(Producer, "publish", unavailable)
                            with pytest.raises(Reject) as rejected:
                                ensure_dispatch_queue(task)
                            assert rejected.value.requeue is False
                    with pytest.raises(Retry):
                        ensure_dispatch_queue(task)
            finally:
                task.pop_request()
        messages = redis_client.lrange(prefix + "resources", 0, -1)
        assert len(messages) == (2 if delivered == "resource-results" else 0)
        for raw in messages:
            message = json.loads(raw)
            assert message["headers"]["id"] == identity
            assert message["headers"]["task"] == "materials.prepare_cover"
            body = json.loads(base64.b64decode(message["body"]))
            assert body[:2] == [[], kwargs]
            assert message["properties"]["delivery_info"]["routing_key"] == "resources"
    finally:
        app.close()
        owned = list(redis_client.scan_iter(prefix + "*"))
        if owned:
            redis_client.delete(*owned)
