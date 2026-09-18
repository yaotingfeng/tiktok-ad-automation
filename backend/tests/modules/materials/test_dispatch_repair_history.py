"""恢复代价随当前业务身份增长，不能随同一身份的历史消息重复查库存。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import repair_material_dispatches
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.modules.materials.test_distribution import queue
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_video_reissue import executable as executable
from tests.modules.materials.test_video_reissue import (
    rejected_material as rejected_material,
)
from tests.modules.materials.test_video_reissue import unknown_video as unknown_video
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def test_completed_history_does_not_repeat_identity_lookups_or_starve_live_repair(
    source_env,
):
    with Session(engine) as db, db.begin():
        finished = target(db, source_env, advertiser_id="completed-history")
        live = target(db, source_env, advertiser_id="live-repair")
        asset(db, source_env, finished, status="result_unknown")
        asset(db, source_env, live, status="result_unknown")
    old_id = queue(source_env, finished).task_id
    active_id = queue(source_env, live).task_id
    with Session(engine) as db, db.begin():
        old = db.get(MaterialDistribution, old_id)
        old.status = "ready"
        db.get(MaterialAssetOperation, old.operation_id).status = "succeeded"
        messages = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id,
            )
        ).all()
        for message in messages:
            message.available_at = message.published_at = datetime.now(UTC) - timedelta(
                minutes=3
            )
        active_message = next(
            row.id
            for row in messages
            if row.payload.get("distribution_id") == str(active_id)
        )
        db.execute(
            text("""
            INSERT INTO pending_dispatch (id,tenant_id,actor_id,task_name,task_key,payload,available_at,published_at,attempts)
            SELECT gen_random_uuid(),:tenant,:actor,'materials.verify_target','old-history:'||n,
                jsonb_build_object('distribution_id',CAST(:distribution AS text),'operation_id',CAST(:operation AS text),'revision',0),
                now()-interval '10 minutes',now()-interval '10 minutes',0
            FROM generate_series(1,20000) n
        """),
            {
                "tenant": source_env["context"].tenant_id,
                "actor": source_env["context"].actor_id,
                "distribution": str(old.id),
                "operation": str(old.operation_id),
            },
        )
        db.flush()
        for table in (
            "pending_dispatch",
            "material_asset_operation",
            "material_distribution",
        ):
            db.execute(text(f"ANALYZE {table}"))
    captured = []

    def capture(_connection, _cursor, statement, parameters, *_rest):
        if "FOR UPDATE" in statement and "pending_dispatch" in statement:
            captured.append((statement, parameters))

    with Session(engine) as db, db.begin():
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert repair_material_dispatches(db, limit=1) == 1
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert db.get(PendingDispatch, active_message).published_at is None
        statement, parameters = captured[-1]
        # 只在独立测试库执行真实 ANALYZE；实际恢复查询包含 LIMIT 与SKIP LOCKED。
        plan = (
            db.connection()
            .exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement,
                parameters,
            )
            .scalar_one()[0]["Plan"]
        )
        identity_lookups = [
            node.get("Actual Loops", 0)
            for node in _nodes(plan)
            if node.get("Relation Name")
            in {"material_distribution", "material_asset_operation"}
        ]
        # 两个业务身份不应因两万条旧消息而执行两万次库存主键查找。
        assert max(identity_lookups, default=0) <= 64, identity_lookups
        # 物化身份可直接按规范字符串连接，不能仍对全部历史消息做UUID正则转换。
        history_conditions = [
            str(node.get(key, ""))
            for node in _nodes(plan)
            for key in ("Hash Cond", "Join Filter", "Filter", "Index Cond")
            if "payload" in str(node.get(key, ""))
        ]
        assert not any(" ~ " in condition for condition in history_conditions)


def test_repair_only_rearms_current_authorized_generation(executable, unknown_video):
    from tests.modules.materials.test_video_reissue import replace

    database, context, _ = executable
    with Session(database) as db, db.begin():
        replacement = replace(db, executable, unknown_video)
        rows = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name.in_(
                    ["materials.prepare_target", "materials.verify_target"]
                ),
            )
        ).all()
        old_ids, new_ids = [], []
        for row in rows:
            row.published_at = row.available_at = datetime.now(UTC) - timedelta(
                minutes=3
            )
            (
                new_ids
                if row.payload.get("operation_id") == replacement["new_operation_id"]
                else old_ids
            ).append(row.id)
        assert old_ids and len(new_ids) == 1
    with Session(database) as db, db.begin():
        assert repair_material_dispatches(db) == 1
        assert all(
            db.get(PendingDispatch, identity).published_at is not None
            for identity in old_ids
        )
        assert db.get(PendingDispatch, new_ids[0]).published_at is None


@pytest.mark.parametrize("changed", ["published_at", "available_at"])
def test_repair_rechecks_due_after_concurrent_dispatch_update(source_env, changed):
    with Session(engine) as db, db.begin():
        account = target(db, source_env, advertiser_id="concurrent-repair")
        asset(db, source_env, account, status="result_unknown")
    identity = queue(source_env, account).task_id
    with Session(engine) as db, db.begin():
        message = next(
            row
            for row in db.exec(select(PendingDispatch)).all()
            if row.payload.get("distribution_id") == str(identity)
        )
        message.published_at = message.available_at = datetime.now(UTC) - timedelta(
            minutes=3
        )
        dispatch_id = message.id
    gate_key = uuid4().int % (2**63 - 1)
    worker_pid = Queue()

    def repair():
        with Session(engine) as db, db.begin():
            connection = db.connection()
            connection.exec_driver_sql(f"""
                CREATE OR REPLACE FUNCTION pg_temp.repair_gate(identity uuid) RETURNS uuid
                LANGUAGE plpgsql VOLATILE AS $$ BEGIN
                    PERFORM pg_advisory_xact_lock({gate_key}); RETURN identity;
                END $$
            """)
            worker_pid.put(
                connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            )

            def wait_before_lock(_connection, _cursor, statement, parameters, *_):
                # 仅在测试连接给真实候选JOIN加同步点，业务谓词/行锁保持原样。
                return statement.replace(
                    "repair_material_candidates.id = pending_dispatch.id",
                    "pg_temp.repair_gate(repair_material_candidates.id) = pending_dispatch.id",
                ), parameters

            event.listen(
                connection, "before_cursor_execute", wait_before_lock, retval=True
            )
            try:
                return repair_material_dispatches(db)
            finally:
                event.remove(connection, "before_cursor_execute", wait_before_lock)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as held, held.begin():
            held.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": gate_key})
            future = pool.submit(repair)
            pid = worker_pid.get(timeout=3)
            deadline = monotonic() + 3
            while True:
                waiting = held.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                    ),
                    {"pid": pid},
                ).scalar_one()
                if waiting:
                    break
                assert monotonic() < deadline, "repair did not reach candidate gate"
                sleep(0.01)
            with Session(engine) as writer, writer.begin():
                row = writer.get(PendingDispatch, dispatch_id)
                setattr(row, changed, datetime.now(UTC) + timedelta(minutes=1))
        assert future.result(timeout=5) == 0
    with Session(engine) as db:
        assert db.get(PendingDispatch, dispatch_id).published_at is not None
