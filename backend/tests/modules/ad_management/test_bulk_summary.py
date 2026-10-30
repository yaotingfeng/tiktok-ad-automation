"""任务汇总不物化万条JSON，不覆盖取消/准备状态。"""

from sqlalchemy import event
from sqlmodel import Session

from app.core.db import engine
from app.modules.ad_management.execution import _update_task
from app.modules.ad_management.models import ManagementTask
from tests.modules.ad_management.test_execution import _committed_item
from tests.modules.ad_management.test_execution import c5_env as c5_env


def test_summary_uses_database_aggregation(c5_env):
    context, task_id, _ = _committed_item(c5_env)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session:
            _update_task(session, task_id, context.tenant_id)
            session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    item_queries = [
        s.lower()
        for s in statements
        if s.startswith("SELECT") and "FROM management_task_item" in s
    ]
    assert any("count(" in s for s in item_queries)
    assert not any("management_task_item.capability" in s for s in item_queries)


def test_summary_does_not_turn_cancelled_into_failed(c5_env):
    context, task_id, _ = _committed_item(c5_env)
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        task.status = "CANCELLED"
        session.add(task)
        session.commit()
        _update_task(session, task_id, context.tenant_id)
        assert task.status == "CANCELLED"


def test_local_quota_deferrals_do_not_exhaust_transport_retries(c5_env, monkeypatch):
    import os
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.core.config import settings
    from app.integrations.tiktok.contracts.management import ManagementReceipt
    from app.modules.ad_management import execution
    from app.modules.ad_management.models import (
        ManagementRequestAttempt,
        ManagementTaskItem,
    )
    from tests.modules.ad_management.test_execution import _Gateway

    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    context, task_id, item_id = _committed_item(c5_env)

    def apply(_self, _command):
        return ManagementReceipt(
            "NOT_SENT", None, True, reason="admission_deferred", retry_after_ms=1000
        )

    monkeypatch.setattr(execution, "open_tiktok_gateway", lambda **kw: _Gateway(apply))
    for _ in range(4):
        execution.execute_item(engine, item_id)
        with Session(engine) as session:
            attempts = session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).all()
            for attempt in attempts:
                attempt.response = {
                    **attempt.response,
                    "retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                }
                session.add(attempt)
            session.commit()
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        attempts = session.exec(
            select(ManagementRequestAttempt)
            .where(ManagementRequestAttempt.task_item_id == item_id)
            .order_by(ManagementRequestAttempt.attempt)
        ).all()
        assert len(attempts) == 4
        assert attempts[-1].retryable
        assert item.execution_result == "NOT_SENT"
        assert session.get(ManagementTask, task_id).status == "RUNNING"


def test_single_item_lock_contention_remains_pending(c5_env, redis_client, monkeypatch):
    import os
    from uuid import uuid4

    from sqlmodel import select

    from app.core.config import settings
    from app.integrations.tiktok.object_coordination import claim_mutation
    from app.modules.ad_management import execution
    from app.modules.ad_management.models import (
        ManagementRequestAttempt,
        ManagementTaskItem,
    )

    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    _, _, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        lease = claim_mutation(
            session, (execution._ref(item.ref),), uuid4(), redis_client=redis_client
        )
    try:
        assert execution.execute_item(engine, item_id) is False
        assert lease.is_current(redis_client)
        with Session(engine) as session:
            assert (
                session.get(ManagementTaskItem, item_id).execution_result == "PENDING"
            )
            assert not session.exec(
                select(ManagementRequestAttempt).where(
                    ManagementRequestAttempt.task_item_id == item_id
                )
            ).all()
    finally:
        lease.release(redis_client)


def test_mutation_lock_order_matches_cancellation(c5_env, monkeypatch):
    import os

    from app.core.config import settings
    from app.integrations.tiktok.contracts.management import ManagementReceipt
    from app.modules.ad_management import execution
    from tests.modules.ad_management.test_execution import _Gateway

    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    _, _, item_id = _committed_item(c5_env)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    monkeypatch.setattr(
        execution,
        "open_tiktok_gateway",
        lambda **kw: _Gateway(lambda *_: ManagementReceipt("NOT_SENT", None, False)),
    )
    event.listen(engine, "before_cursor_execute", capture)
    try:
        execution.execute_item(engine, item_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert "FROM management_task \n" in statements[0]
