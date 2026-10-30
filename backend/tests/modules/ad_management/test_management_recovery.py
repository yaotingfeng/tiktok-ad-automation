"""重投发生在未来到期前，仍必须留下持久化唤醒，不能ACK后丢任务。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.integrations.tiktok.contracts.management import ManagementReceipt
from app.jobs.models import PendingDispatch
from app.modules.ad_management import execution
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTaskItem,
)
from app.modules.ad_management.tasks import execute_management_task
from tests.modules.ad_management.test_execution import _committed_item, _Gateway
from tests.modules.ad_management.test_execution import c5_env as c5_env


def dispatch(context, task_id):
    return execute_management_task(
        tenant_id=str(context.tenant_id),
        actor_id=str(context.actor_id),
        payload={"task_id": str(task_id), "generation": 1},
    )


def test_retry_before_due_recreates_missing_recovery_outbox(c5_env, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    context, task_id, item_id = _committed_item(c5_env)

    def apply(_self, _cmd):
        return ManagementReceipt(
            "NOT_SENT", None, True, reason="admission_deferred", retry_after_ms=300000
        )

    monkeypatch.setattr(execution, "open_tiktok_gateway", lambda **kw: _Gateway(apply))
    execution.execute_item(engine, item_id)
    with Session(engine) as session:
        for row in session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        ).all():
            session.delete(row)
        session.commit()
    dispatch(context, task_id)
    with Session(engine) as session:
        wakes = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.available_at > datetime.now(UTC),
            )
        ).all()
        assert wakes


def test_live_claim_redelivery_keeps_expiry_watchdog(c5_env, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", os.environ["TEST_REDIS_URL"])
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        item.claim_token = uuid4()
        item.claimed_until = datetime.now(UTC) + timedelta(seconds=120)
        item.delivery_status = "ATTEMPT_RECORDED"
        session.add(item)
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id,
                task_item_id=item_id,
                attempt=1,
                outcome="NOT_SENT",
                retryable=False,
            )
        )
        session.commit()
    dispatch(context, task_id)
    with Session(engine) as session:
        assert session.get(ManagementTaskItem, item_id).execution_result == "PENDING"
        assert session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.available_at > datetime.now(UTC),
            )
        ).all()


def test_cancelled_with_refresh_pending_does_not_reactivate_writes(c5_env, monkeypatch):
    from app.modules.ad_management.models import ManagementTask
    from tests.modules.ad_management.test_bulk_execution import make_items

    context, task_id, ids = make_items(c5_env, 2)
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        task.status = "CANCELLED"
        first, second = (session.get(ManagementTaskItem, identity) for identity in ids)
        first.execution_result, first.observation_state = "ACCEPTED", "REFRESH_PENDING"
        second.execution_result = "NOT_SENT"
        session.add_all([task, first, second])
        session.commit()
    calls = []
    monkeypatch.setattr(
        execution, "execute_item", lambda _engine, identity: calls.append(identity)
    )
    dispatch(context, task_id)
    assert calls == [ids[0]]
    with Session(engine) as session:
        assert session.get(ManagementTask, task_id).status == "CANCELLED"
