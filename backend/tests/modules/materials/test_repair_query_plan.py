"""真实 PostgreSQL 计划：候选去重后按消息 UUID 加锁，不逐条重查历史库存。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import repair_material_dispatches
from tests.modules.materials.test_distribution import queue
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_repair_locks_candidate_dispatch_by_primary_key(source_env, wire):
    with Session(engine) as db, db.begin():
        account = target(db, source_env)
        asset(db, source_env, account, status="result_unknown")
    queued = queue(source_env, account)
    assert queued.task_id is not None, queued
    captured = []

    def capture(_connection, _cursor, statement, parameters, *_rest):
        if "FOR UPDATE" in statement and "pending_dispatch" in statement:
            captured.append((statement, parameters))

    with Session(engine) as db, db.begin():
        row = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).one()
        row.published_at = row.available_at = datetime.now(UTC) - timedelta(minutes=3)
        db.flush()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert repair_material_dispatches(db) == 1
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        statement, parameters = captured[-1]
        # 测试库里 pending_dispatch 只有寥寥几行；一旦 autovacuum 统计为近空表，
        # 规划器会改用按主键顺序全扫的 Merge/Hash Join，与线上历史消息量下的
        # 计划不同。这里与禁用顺序扫描同理，模拟大表代价，只检查按主键探测是否可行。
        db.execute(text("SET LOCAL enable_seqscan = off"))
        db.execute(text("SET LOCAL enable_mergejoin = off"))
        db.execute(text("SET LOCAL enable_hashjoin = off"))
        plan = (
            db.connection()
            .exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters)
            .scalar_one()
        )

        def nodes(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nodes(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nodes(child)

        # 只看最外层加锁节点之下的扫描：候选 CTE 内部的主键探测不能代替
        # 实际 FOR UPDATE 行按消息主键定位。
        lock_rows = [
            node for node in nodes(plan) if node.get("Node Type") == "LockRows"
        ]
        assert len(lock_rows) == 1, plan
        indexed = [
            node
            for node in nodes(lock_rows[0])
            if node.get("Relation Name") == "pending_dispatch" and "Index Cond" in node
        ]
        assert any("id =" in node["Index Cond"] for node in indexed), indexed
        assert db.execute(text("SHOW statement_timeout")).scalar_one() == "10s"
        assert db.execute(text("SHOW jit")).scalar_one() == "off"
    assert wire[0] == []


@pytest.mark.parametrize("invalid", ["not-a-uuid", "", None, {"id": "bad"}])
def test_malformed_old_reference_cannot_abort_repair(source_env, invalid):
    with Session(engine) as db, db.begin():
        db.add(
            PendingDispatch(
                tenant_id=source_env["context"].tenant_id,
                actor_id=source_env["context"].actor_id,
                task_name="materials.verify_target",
                task_key=f"bad:{uuid4()}",
                payload={"distribution_id": invalid, "operation_id": invalid},
                published_at=datetime.now(UTC) - timedelta(minutes=5),
                available_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        db.flush()
        assert repair_material_dispatches(db) == 0
