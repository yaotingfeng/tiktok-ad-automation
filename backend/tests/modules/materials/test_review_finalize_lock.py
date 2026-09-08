"""Regression: genuine concurrent deliveries cannot reverse file/operation locks."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.materials import source_uploads
from app.modules.materials.models import MaterialAssetOperation
from tests.modules.materials.test_source_uploads import run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_duplicate_delivery_during_verified_insert_must_not_deadlock(
    source_env, redis_client, wire, monkeypatch
):
    from tests.modules.materials.test_source_uploads import info, seed_operation

    op_id = seed_operation(source_env)
    locked_operation, load_material = (
        source_uploads._locked_operation,
        source_uploads._material,
    )
    sender_locked, duplicate_entered = Event(), Event()
    duplicate_pid, codes = [], []
    intercepted = []

    def record_error(context):
        codes.append(getattr(context.original_exception, "sqlstate", None))

    def material(session, *args, **kwargs):
        if current_thread().name.endswith("_1") and not duplicate_pid:
            duplicate_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            duplicate_entered.set()
        return load_material(session, *args, **kwargs)

    def operation(session, context, operation_id):
        result = locked_operation(session, context, operation_id)
        name = current_thread().name
        if name.endswith("_0"):
            intercepted.append(name)
        if name.endswith("_0") and len(intercepted) == 2:
            sender_locked.set()
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
                    raise AssertionError("Duplicate never reached its row lock")
        return result

    monkeypatch.setattr(source_uploads, "_material", material)
    monkeypatch.setattr(source_uploads, "_locked_operation", operation)
    wire[1].append(info())
    event.listen(engine, "handle_error", record_error)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="review") as pool:
            sender = pool.submit(
                run, source_env, redis_client, kind="verify", operation_id=op_id
            )
            assert sender_locked.wait(5)
            duplicate = pool.submit(
                run, source_env, redis_client, kind="verify", operation_id=op_id
            )
            sender.result(10)
            duplicate.result(10)
    finally:
        event.remove(engine, "handle_error", record_error)
    assert "40P01" not in codes
    with Session(engine) as session:
        operation = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == source_env["material_id"]
            )
        ).one()
        assert operation.status == "succeeded", operation.remote_response
    assert len(wire[0]) == 1
