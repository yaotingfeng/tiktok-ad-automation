"""真实 PostgreSQL 计划：恢复查找必须按 UUID 主键，而非逐条扫描租户库存。"""

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


def test_repair_uses_material_primary_key_in_actual_postgres_plan(source_env, wire):
    with Session(engine) as db, db.begin():
        account = target(db, source_env)
        asset(db, source_env, account, seconds_old=1000)
    queued = queue(source_env, account)
    assert queued.task_id is not None, queued
    captured = []

    def capture(_connection, _cursor, statement, parameters, *_rest):
        if statement.startswith("SELECT pending_dispatch.id") and "EXISTS" in statement:
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
        db.execute(text("SET LOCAL enable_seqscan = off"))
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

        indexed = [
            node
            for node in nodes(plan)
            if node.get("Relation Name") == "material_distribution"
            and "Index Cond" in node
        ]
        assert any("id = CASE" in node["Index Cond"] for node in indexed), indexed
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
