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
from tests.query_plans import explain_prepared


@pytest.mark.parametrize("cached", [False, True])
def test_repair_locks_candidate_dispatch_by_primary_key(source_env, wire, cached):
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
        # 用历史报表消息模拟生产大 outbox，恢复成本应跟活跃素材身份走。
        db.connection().execute(
            text("""
            INSERT INTO pending_dispatch (id, tenant_id, actor_id, task_name, task_key,
                payload, available_at, published_at, attempts)
            SELECT gen_random_uuid(), :tenant, :actor,
                (ARRAY['reporting.job_step','materials.upload_original','materials.verify_original',
                  'materials.prepare_target','materials.verify_target'])[1+n%5],
                :prefix || n::text, jsonb_build_object('operation_id', gen_random_uuid()::text,
                  'distribution_id', gen_random_uuid()::text),
                now()-interval '1 day', now()-interval '1 day', 0
            FROM generate_series(1, 5000) n
        """),
            {
                "tenant": row.tenant_id,
                "actor": row.actor_id,
                "prefix": f"history:{uuid4()}:",
            },
        )
        db.execute(text("ANALYZE pending_dispatch"))
        if cached:
            db.execute(text("SET LOCAL plan_cache_mode='force_generic_plan'"))
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
        db.execute(text("SET LOCAL enable_bitmapscan = off"))
        plan = (
            explain_prepared(db.connection(), statement, parameters)
            if cached
            else (
                db.connection()
                .exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters)
                .scalar_one()
            )
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
        # 主键探测可能是 Index Scan，也可能是位图扫描（条件在子节点 Bitmap Index Scan，
        # 子节点不带 Relation Name）；两者都是按 pending_dispatch_pkey 定位单行。
        indexed = [
            node
            for node in nodes(lock_rows[0])
            if "Index Cond" in node
            and (
                node.get("Relation Name") == "pending_dispatch"
                or node.get("Index Name") == "pending_dispatch_pkey"
            )
        ]
        # 失败时输出加锁节点下的完整扫描链，便于区分统计漂移与真实回退。
        chain = [
            (
                n.get("Node Type"),
                n.get("Relation Name"),
                n.get("Index Name"),
                n.get("Index Cond") or n.get("Filter"),
            )
            for n in nodes(lock_rows[0])
            if "Node Type" in n
        ]
        assert any("id =" in node["Index Cond"] for node in indexed), chain
        # 源/目标候选必须按活跃 operation 身份探测消息，不能每轮全扫历史 outbox。
        operation_probes = [
            n
            for n in nodes(plan)
            if n.get("Relation Name") == "pending_dispatch"
            and "operation_id" in n.get("Index Cond", "")
        ]
        assert len(operation_probes) >= 2, chain
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
