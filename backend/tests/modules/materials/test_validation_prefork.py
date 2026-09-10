"""Linux hard-kill evidence; macOS runs only the real-PG ownership companion.

The local transport stalls on a Redis blocking read after a durable validation
use exists. This exercises the original validator task/guard and byte reader,
not a real R2 endpoint or a TikTok upload. A duplicate validator is not evidence
that an earlier reader stopped; cancellation must retain its reservation.

Only the test wrapper's deadline is reduced to three seconds. The production
handler and guard run unchanged. This does not prove hard-killed temporary-file
cleanup or source-upload UNKNOWN reconciliation; no source upload is started.
"""

import os
import sys
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlmodel import Session, select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.materials import object_validation, validation_tasks
from app.modules.materials.cleanup import run_cleanup
from app.modules.materials.ingest_models import (
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
)
from app.modules.materials.ingest_schemas import IngestIdentity
from app.modules.materials.ingest_transport import cancel_file
from app.modules.materials.models import MaterialAssetOperation
from app.modules.materials.object_budget import mark_object_stored, reserve_object
from app.modules.materials.object_uses import acquire_original_use
from tests.modules.materials.test_ingest_models import original
from tests.modules.materials.test_validation_dispatch import ingest_fixture
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


class StalledLocalStorage:
    """Exact metadata; only its body read waits on this test's Redis namespace."""

    def __init__(self, obj, broker_url, prefix):
        self.obj, self.broker_url, self.prefix = obj, broker_url, prefix

    def head_object(self, **values):
        assert values == {"Bucket": self.obj.storage_bucket, "Key": self.obj.object_key}
        return {
            "ContentLength": self.obj.expected_bytes,
            "ContentType": "video/mp4",
            "Metadata": {
                "tenant-id": str(self.obj.tenant_id),
                "bc-id": self.obj.bc_id,
                "material-id": str(self.obj.material_id),
                "generation": str(self.obj.generation),
            },
        }

    def get_object(self, **values):
        with Redis.from_url(self.broker_url) as client:
            client.incr(self.prefix + "gets")
        return {**self.head_object(**values), "Body": self}

    def read(self, size):
        assert 0 < size <= 8 * 1024 * 1024
        with Redis.from_url(self.broker_url) as client:
            client.set(self.prefix + "reading", str(os.getpid()), ex=120)
            if client.get(self.prefix + "mode") == b"fail":
                raise OSError("synthetic completed local read failure")
            client.blpop(self.prefix + "release", timeout=30)
        raise OSError("synthetic stalled read released")

    def close(self):
        pass


@pytest.fixture
def validation_runtime(isolated_strategy_database, redis_client, monkeypatch, tmp_path):
    database_engine, context, _ = isolated_strategy_database
    prefix = f"validator-prefork-{uuid4().hex}:"
    broker_url = os.environ["TEST_REDIS_URL"]
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    monkeypatch.setattr(validation_tasks, "engine", database_engine)
    monkeypatch.setattr(
        object_validation,
        "TemporaryDirectory",
        partial(TemporaryDirectory, dir=tmp_path),
    )
    with Session(database_engine) as db, db.begin():
        batch, _, materials = ingest_fixture(db, context)
        material = materials[0]
        obj = original(
            db,
            context,
            material,
            storage_provider="r2",
            storage_endpoint="https://synthetic.r2.cloudflarestorage.com",
            storage_bucket="prefork-owned",
        )
        material.current_object_generation = obj.generation
        assert reserve_object(
            db, context=context, object_id=obj.id, byte_size=obj.expected_bytes
        )
        mark_object_stored(
            db, context=context, object_id=obj.id, actual_bytes=obj.expected_bytes
        )
        dispatch_id = object_validation.enqueue_validation(
            db, context=context, object_id=obj.id
        )
        dispatch = db.get(PendingDispatch, dispatch_id)
        payload = dict(dispatch.payload)
        identities = obj.id, material.id, batch.id

    def transport(obj):
        return StalledLocalStorage(obj, broker_url, prefix)

    def forbid_sdk(*_args, **_kwargs):
        with Redis.from_url(broker_url) as client:
            client.incr(prefix + "sdk_calls")
        raise AssertionError("Validator deadline tests forbid TikTok transport")

    monkeypatch.setattr(object_validation, "make_object_s3", transport)
    monkeypatch.setattr("urllib3.PoolManager.request", forbid_sdk)
    try:
        yield {
            "db": database_engine,
            "context": context,
            "object_id": identities[0],
            "material_id": identities[1],
            "session_id": identities[2],
            "dispatch_id": dispatch_id,
            "payload": payload,
            "prefix": prefix,
            "broker_url": broker_url,
        }
    finally:
        # Only this test's random broker/transport keys; DB fixture owns its DB.
        owned = list(redis_client.scan_iter(match=prefix + "*"))
        if owned:
            redis_client.delete(*owned)


def validate(case):
    object_validation.validate_original(
        database_engine=case["db"],
        context=case["context"],
        object_id=case["object_id"],
        dispatch_id=case["dispatch_id"],
        generation=case["payload"]["generation"],
        revision=case["payload"]["revision"],
    )


def assert_cancel_preserves_unknown_use(case, old_use_id, redis_client):
    with Session(case["db"]) as db:
        obj = db.get(TemporaryMaterialObject, case["object_id"])
        identity = IngestIdentity(
            generation=obj.generation,
            upload_id=obj.s3_upload_id,
            operation_revision=obj.revision,
        )
    cancel_file(
        database_engine=case["db"],
        context=case["context"],
        session_id=case["session_id"],
        material_id=case["material_id"],
        identity=identity,
    )
    with Session(case["db"]) as db:
        cleanup_id = db.exec(
            select(ObjectCleanup.id).where(
                ObjectCleanup.material_id == case["material_id"]
            )
        ).one()
    calls = []

    class NoDelete:
        def delete_object(self, **_values):
            calls.append("delete")
            raise AssertionError("Unknown validation use must block deletion")

    run_cleanup(database_engine=case["db"], cleanup_id=cleanup_id, s3=NoDelete())
    with Session(case["db"]) as db:
        assert db.get(ObjectCleanup, cleanup_id).error_code == "original_in_use"
        assert db.get(OriginalUse, old_use_id).status == "active"
        obj = db.get(TemporaryMaterialObject, case["object_id"])
        assert obj.reserved_bytes == obj.expected_bytes == 5
        for key in ("global", f"tenant:{case['context'].tenant_id}"):
            budget = db.get(ObjectBudget, key)
            assert budget.reserved_bytes == budget.stored_bytes == 5
        assert (
            db.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.material_id == case["material_id"]
                )
            ).all()
            == []
        )
        assert (
            db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "materials.upload_original"
                )
            ).all()
            == []
        )
    assert calls == [] and not redis_client.exists(case["prefix"] + "sdk_calls")


def test_failed_successor_releases_only_its_exact_validation_use(
    validation_runtime, redis_client
):
    """Cross-platform PG companion; does not claim a real killed process."""
    case = validation_runtime
    with Session(case["db"]) as db, db.begin():
        use = acquire_original_use(
            db,
            context=case["context"],
            object_id=case["object_id"],
            purpose="validation",
            operation_id=uuid4(),
            dispatch_id=case["dispatch_id"],
            lifetime_seconds=30,
        )
        use.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        old_use_id = use.id
    redis_client.set(case["prefix"] + "mode", "fail", ex=120)
    validate(case)
    with Session(case["db"]) as db:
        uses = db.exec(
            select(OriginalUse).where(OriginalUse.material_id == case["material_id"])
        ).all()
        assert len(uses) == 2
        assert {use.status for use in uses} == {"active", "released"}
    assert_cancel_preserves_unknown_use(case, old_use_id, redis_client)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Actual prefork hard-kill evidence runs on Linux CI"
)
def test_validator_hard_kill_and_exact_redelivery_keep_unknown_use(
    validation_runtime, redis_client
):
    case = validation_runtime
    prefix, database_engine = case["prefix"], case["db"]
    queue = prefix + "resources"
    app = Celery(prefix, broker=case["broker_url"], set_as_current=False)
    app.conf.update(
        task_default_queue=queue,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        # Deterministic explicit redelivery below; do not race an automatic
        # timeout redelivery when asserting that the killed handler did not return.
        task_acks_on_failure_or_timeout=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"global_keyprefix": prefix},
        worker_hijack_root_logger=False,
    )
    production_body = validation_tasks.validate_original_task.run.__func__

    @app.task(
        name=prefix + "validate_original",
        bind=True,
        shared=False,
        time_limit=3,
        soft_time_limit=None,
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def task(self, **kwargs):
        database_engine.dispose(close=False)
        # Original production task function: its own guard and exact payload/ID checks.
        production_body(self, **kwargs)
        with Redis.from_url(case["broker_url"]) as client:
            client.incr(prefix + "returned")

    def deliver():
        task.apply_async(
            task_id=str(case["dispatch_id"]),
            kwargs={
                "tenant_id": str(case["context"].tenant_id),
                "actor_id": str(case["context"].actor_id),
                "payload": case["payload"],
            },
        )

    def wait_for(predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert predicate(), "Bounded prefork observation did not arrive"

    try:
        with start_worker(
            app,
            pool="prefork",
            concurrency=1,
            perform_ping_check=False,
            shutdown_timeout=15,
            queues=[queue],
            loglevel="ERROR",
        ) as worker:
            original_pid = worker.pool._pool._pool[0].pid
            deliver()
            wait_for(lambda: redis_client.exists(prefix + "reading"), seconds=10)
            assert int(redis_client.get(prefix + "reading")) == original_pid
            with Session(database_engine) as db:
                use = db.exec(
                    select(OriginalUse).where(
                        OriginalUse.material_id == case["material_id"]
                    )
                ).one()
                assert use.status == "active" and use.dispatch_id == case["dispatch_id"]
                old_use_id = use.id
            wait_for(
                lambda: (
                    bool(worker.pool._pool._pool)
                    and original_pid
                    not in [child.pid for child in tuple(worker.pool._pool._pool)]
                )
            )
            assert not redis_client.exists(prefix + "returned")
            deliver()  # Same durable dispatch while the original owner lease is live.
            wait_for(lambda: redis_client.exists(prefix + "returned"))
            assert int(redis_client.get(prefix + "gets")) == 1
            with Session(database_engine) as db, db.begin():
                obj = db.get(TemporaryMaterialObject, case["object_id"])
                assert obj.status == "validating"
                assert db.get(OriginalUse, old_use_id).status == "active"
                # Advance only this owned test's lease/URL clocks to exercise recovery.
                obj.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
                db.get(OriginalUse, old_use_id).expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
            before = int(redis_client.get(prefix + "returned"))
            redis_client.set(prefix + "mode", "fail", ex=120)
            deliver()
            wait_for(lambda: int(redis_client.get(prefix + "returned") or 0) > before)
            assert int(redis_client.get(prefix + "gets")) == 2
            with Session(database_engine) as db:
                uses = db.exec(
                    select(OriginalUse).where(
                        OriginalUse.material_id == case["material_id"]
                    )
                ).all()
                assert len(uses) == 2
                assert {use.status for use in uses} == {"active", "released"}
            assert_cancel_preserves_unknown_use(case, old_use_id, redis_client)
    finally:
        redis_client.lpush(prefix + "release", "stop")
        app.close()
