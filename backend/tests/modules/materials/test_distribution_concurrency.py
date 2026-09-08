from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.materials.distribution import repair_material_dispatches
from app.modules.materials.models import (
    MaterialAssetOperation,
    MaterialFile,
    ObjectUpload,
)
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_source_uploads import info, seed_operation
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_concurrent_build_consumers_share_one_pending_distribution(source_env, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    gate = Barrier(2)

    def submit():
        gate.wait(5)
        return queue(source_env, account).task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(submit), pool.submit(submit)
        assert a.result(5) == b.result(5)
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == source_env["context"].tenant_id
                    )
                ).all()
            )
            == 1
        )
    assert wire[0] == []


def test_concurrent_workers_send_one_target_upload_and_hold_no_db_locks(
    source_env, redis_client, wire, original_s3
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    dist_id = queue(source_env, account).task_id
    entered, finish = Event(), Event()

    def response():
        with Session(engine) as session, session.begin():
            op = session.exec(
                select(MaterialAssetOperation)
                .where(MaterialAssetOperation.id == state(dist_id)[1].id)
                .with_for_update(nowait=True)
            ).one()
            session.exec(
                select(MaterialFile)
                .where(MaterialFile.id == source_env["material_id"])
                .with_for_update(nowait=True)
            ).one()
            assert op.status == "sending" and op.attempt_token
        entered.set()
        assert finish.wait(5)
        return [{"video_id": "received-target"}]

    wire[1].append(response)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run, source_env, redis_client, dist_id, kind="prepare", s3=original_s3[0]
        )
        assert entered.wait(5)
        second = pool.submit(
            run, source_env, redis_client, dist_id, kind="prepare", s3=original_s3[0]
        )
        second.result(5)
        finish.set()
        first.result(5)
    assert len(wire[0]) == 1 and state(dist_id)[1].status == "verifying"


def test_old_target_completion_cannot_publish_after_fence_changes(
    source_env, redis_client, wire
):
    from tests.modules.materials.test_readiness import asset

    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=1000)
    dist_id = queue(source_env, account).task_id

    def response():
        with Session(engine) as session, session.begin():
            op = session.get(MaterialAssetOperation, state(dist_id)[1].id)
            op.attempt_token = uuid4()
        return info(vid="should-not-publish")

    wire[1].append(response)
    run(source_env, redis_client, dist_id)
    assert state(dist_id)[2].video_id != "should-not-publish"


def test_lost_target_message_repairs_same_id_without_new_generation(
    source_env, redis_client, wire, original_s3
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    dist_id = queue(source_env, account).task_id
    with Session(engine) as session, session.begin():
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).one()
        identity, payload = dispatch.id, dict(dispatch.payload)
        dispatch.published_at = dispatch.available_at = datetime.now(UTC) - timedelta(
            minutes=3
        )
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session) == 1
    with Session(engine) as session:
        dispatch = session.get(PendingDispatch, identity)
        assert dispatch.published_at is None and dispatch.payload == payload
    wire[1].append([{"video_id": "received"}])
    run(
        source_env,
        redis_client,
        dist_id,
        kind="prepare",
        s3=original_s3[0],
        revision=payload["revision"],
    )
    wire[1].append(info())
    run(source_env, redis_client, dist_id)
    assert state(dist_id)[0].status == "ready"


def test_stale_source_rows_do_not_starve_current_repair_window(source_env):
    op_id = seed_operation(source_env)
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.remote_response = {**op.remote_response, "revision": 5}
        records = []
        for index in range(206):
            record = PendingDispatch(
                tenant_id=source_env["context"].tenant_id,
                actor_id=source_env["context"].actor_id,
                task_name="materials.verify_original",
                task_key=f"fixture:{uuid4()}",
                payload={
                    "material_id": str(source_env["material_id"]),
                    "operation_id": str(op_id),
                    "revision": 5 if index == 205 else 0,
                },
                available_at=datetime.now(UTC) - timedelta(minutes=5),
                published_at=datetime.now(UTC)
                - timedelta(minutes=5 if index < 205 else 3),
            )
            session.add(record)
            records.append(record)
        session.flush()
        current_id = records[-1].id
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session, limit=1) == 1
        assert session.get(PendingDispatch, current_id).published_at is None
        assert repair_material_dispatches(session, limit=1) == 0


def test_lost_initial_source_dispatch_recovers_before_first_claim(source_env):
    with Session(engine) as session, session.begin():
        identity = enqueue_after_commit(
            session,
            context=source_env["context"],
            task_name="materials.upload_original",
            task_key=f"fixture-original:{uuid4()}",
            payload={"material_id": str(source_env["material_id"])},
        )
        upload = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.material_id == source_env["material_id"]
            )
        ).one()
        upload.task_id = identity
        dispatch = session.get(PendingDispatch, identity)
        dispatch.published_at = dispatch.available_at = datetime.now(UTC) - timedelta(
            minutes=3
        )
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session) == 1
        assert session.get(PendingDispatch, identity).published_at is None


def test_active_source_claim_and_unpublished_backoff_are_untouched(source_env):
    op_id = seed_operation(source_env)
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.attempt_token = uuid4()
        op.claimed_until = datetime.now(UTC) + timedelta(minutes=15)
        watchdog = PendingDispatch(
            tenant_id=source_env["context"].tenant_id,
            actor_id=source_env["context"].actor_id,
            task_name="materials.verify_original",
            task_key=f"fixture-watch:{uuid4()}",
            payload={
                "material_id": str(source_env["material_id"]),
                "operation_id": str(op_id),
                "claim_id": str(op.attempt_token),
            },
            available_at=datetime.now(UTC) - timedelta(minutes=5),
            published_at=datetime.now(UTC) - timedelta(minutes=3),
        )
        session.add(watchdog)
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session) == 0


def test_source_observer_keeps_one_unpublished_message_and_its_due_time(
    source_env, redis_client, wire
):
    seed_operation(source_env)
    dist_id = queue(source_env, "actual-account").task_id
    with Session(engine) as session, session.begin():
        dispatch = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).one()
        identity = dispatch.id
        dispatch.published_at = datetime.now(UTC)
    run(source_env, redis_client, dist_id)
    with Session(engine) as session:
        due = session.get(PendingDispatch, identity).available_at
    for _ in range(3):
        run(source_env, redis_client, dist_id)
    with Session(engine) as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).all()
        assert len(rows) == 1 and rows[0].id == identity and rows[0].available_at == due
    assert not wire[0]


def test_target_finalization_locks_file_before_operation_and_fk_insert(
    source_env, redis_client, wire, monkeypatch
):
    import time
    from threading import current_thread

    from sqlalchemy import event, text

    from app.modules.materials import distribution
    from tests.modules.materials.test_readiness import asset

    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=1000)
    dist_id = queue(source_env, account).task_id
    with Session(engine) as session, session.begin():
        # Require a new AccountMaterial FK insertion at finalization.
        mapping = state(dist_id)[2]
        session.delete(session.get(type(mapping), mapping.id))
    lock_operation, load_material = (
        distribution._locked_operation,
        distribution.load_material,
    )
    locked, duplicate_entered = Event(), Event()
    counts, duplicate_pid, codes = [], [], []

    def record_error(context):
        codes.append(getattr(context.original_exception, "sqlstate", None))

    def load(session, *args, **kwargs):
        if current_thread().name.endswith("_1") and not duplicate_pid:
            duplicate_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            duplicate_entered.set()
        return load_material(session, *args, **kwargs)

    def lock(session, context, operation_id):
        result = lock_operation(session, context, operation_id)
        if current_thread().name.endswith("_0"):
            counts.append(1)
        if current_thread().name.endswith("_0") and len(counts) == 3:
            locked.set()
            assert duplicate_entered.wait(5)
            until = time.monotonic() + 5
            with Session(engine) as observer:
                while time.monotonic() < until:
                    waiting = observer.execute(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": duplicate_pid[0]},
                    ).scalar_one()
                    observer.rollback()
                    if waiting == "Lock":
                        break
                    time.sleep(0.005)
                else:
                    raise AssertionError("Duplicate did not wait on file lock")
        return result

    monkeypatch.setattr(distribution, "load_material", load)
    monkeypatch.setattr(distribution, "_locked_operation", lock)
    wire[1].append(info(vid="verified-target"))
    event.listen(engine, "handle_error", record_error)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="target") as pool:
            sender = pool.submit(run, source_env, redis_client, dist_id)
            assert locked.wait(5)
            duplicate = pool.submit(run, source_env, redis_client, dist_id)
            sender.result(10)
            duplicate.result(10)
    finally:
        event.remove(engine, "handle_error", record_error)
    assert "40P01" not in codes and len(wire[0]) == 1
    assert state(dist_id)[0].status == "ready"
    assert state(dist_id)[2].video_id == "verified-target"


def test_revoked_source_read_successor_can_be_rearmed_without_upload_retry(
    source_env, redis_client, wire
):
    import pytest

    from app.core.errors import DomainError
    from app.modules.tenants.models import TenantMembership
    from tests.modules.materials.test_source_uploads import run as source_run

    op_id = seed_operation(source_env, status="result_unknown")
    context = source_env["context"]
    with Session(engine) as session, session.begin():
        dispatch = PendingDispatch(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            task_name="materials.verify_original",
            task_key=f"revoked-read:{uuid4()}",
            payload={
                "material_id": str(source_env["material_id"]),
                "operation_id": str(op_id),
                "revision": 0,
            },
            available_at=datetime.now(UTC) - timedelta(minutes=3),
            published_at=datetime.now(UTC) - timedelta(minutes=3),
        )
        session.add(dispatch)
        session.flush()
        dispatch_id = dispatch.id
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "viewer"
    with pytest.raises(DomainError):
        source_run(source_env, redis_client, operation_id=op_id, revision=0)
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session) == 1
        assert session.get(PendingDispatch, dispatch_id).published_at is None
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "operator"
    wire[1].append(info())
    source_run(source_env, redis_client, operation_id=op_id, revision=0)
    with Session(engine) as session:
        assert session.get(MaterialAssetOperation, op_id).status == "succeeded"
    assert len(wire[0]) == 1 and wire[0][0][0] == "GET"


def test_source_upload_and_same_target_distribution_race_share_send_authority(
    source_env, redis_client, wire, original_s3
):
    from tests.modules.materials.test_source_uploads import run as source_run

    dist_id = queue(source_env, "actual-account").task_id
    gate = Barrier(2)
    wire[1].append([{"video_id": "one-upload"}])

    def source():
        gate.wait(5)
        source_run(source_env, redis_client, kind="upload", s3=original_s3[0])

    def target_worker():
        gate.wait(5)
        run(source_env, redis_client, dist_id, kind="prepare", s3=original_s3[0])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(source), pool.submit(target_worker)
        first.result(10)
        second.result(10)
    assert len(wire[0]) == 1 and wire[0][0][0] == "POST"
    assert state(dist_id)[1].status == "verifying"
