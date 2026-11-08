"""真实PG执行计划验证：纯唤醒合同不能把主键候选页扩大为前缀扫描。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.sync_control.retention import compact_sync_dispatches
from tests.modules.sync_control.test_balance_dispatch_scale import capture_plans


def test_planning_contract_checks_stay_inside_locked_candidate_page(session, sync_seed):
    now = datetime.now(UTC)
    # 大量同前缀历史使tenant/key复合索引成为诱人的错误路径；真正候选只有一页。
    template = {
        "tenant_id": sync_seed.tenant_id,
        "actor_id": sync_seed.actor_id,
        "task_name": "reporting.plan_due",
        "payload": {"dispatch_only": True},
        "published_at": now - timedelta(days=8),
        "available_at": now - timedelta(days=9),
    }
    rows = [dict(template, id=uuid4(), task_key=f"ordinary:{n}") for n in range(20000)]
    # 第一页不能清理，后面大量可清理正文不能被拿来凑满LIMIT。
    for row in rows[:1250]:
        row.update(
            published_at=now - timedelta(days=10), payload={"dispatch_only": False}
        )
    session.execute(PendingDispatch.__table__.insert(), rows)
    session.execute(text("ANALYZE pending_dispatch"))
    # 模拟生产中前缀选择率低估：统计采样后相同租户产生大量纯唤醒。
    session.execute(
        text(
            "UPDATE pending_dispatch SET task_key='sync-health:' || task_key WHERE tenant_id=:tenant"
        ),
        {"tenant": sync_seed.tenant_id},
    )
    plans = capture_plans(
        session,
        lambda sql: (
            "FOR UPDATE" in sql
            and "dispatch_only" not in sql
            and "pending_dispatch" in sql
        ),
        lambda: compact_sync_dispatches(
            session, limit=1250, cutoff=now - timedelta(days=7)
        ),
    )
    assert plans

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    for plan in plans:
        for node in nodes(plan["Plan"]):
            if node.get("Relation Name") == "pending_dispatch":
                assert "task_key" not in node.get("Filter", ""), (
                    "前缀合同必须在候选页外核验，不能成为基表访问路径"
                )
                assert (
                    node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)
                    <= 1250
                )


def test_locked_planning_page_skips_and_revisits_without_losing_redis_cursor(
    committed_sync_seed, redis_client, monkeypatch
):
    from app.core.config import settings
    from app.jobs.tasks import compact_dispatches_task

    seed, factory = committed_sync_seed
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    cursor_key = "sync:legacy-dispatch-compaction:cursor:v1"
    redis_client.delete(cursor_key)
    now = datetime.now(UTC)
    ids = [uuid4(), uuid4()]
    with factory() as db, db.begin():
        for index, identity in enumerate(ids):
            db.add(
                PendingDispatch(
                    id=identity,
                    tenant_id=seed.tenant_id,
                    actor_id=seed.actor_id,
                    task_name="reporting.plan_due",
                    task_key=f"sync-health:{identity}",
                    payload={"dispatch_only": True},
                    published_at=now - timedelta(days=10 - index),
                )
            )
    try:
        with factory() as locker, locker.begin():
            locker.exec(
                select(PendingDispatch)
                .where(PendingDispatch.id == ids[0])
                .with_for_update()
            ).one()
            # 真实第二连接必须跳过第一页的锁，允许另一条完成，不能等待到超时。
            assert compact_dispatches_task.run(limit=8) == 1
            assert redis_client.get(cursor_key)
            with factory() as db:
                assert db.get(PendingDispatch, ids[0]).payload == {
                    "dispatch_only": True
                }
                assert db.get(PendingDispatch, ids[1]).payload == {}
        # 游标扫到末尾后重访被锁行，旧幂等身份始终保留。
        assert compact_dispatches_task.run(limit=8) == 0
        assert compact_dispatches_task.run(limit=8) == 1
        with factory() as db:
            assert all(
                db.get(PendingDispatch, identity).compacted_at for identity in ids
            )
    finally:
        redis_client.delete(cursor_key)
