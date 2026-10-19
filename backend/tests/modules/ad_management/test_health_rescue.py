"""持久监控只重投原已授权动作，不重建广告请求或重置业务状态。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ad_management.models import ManagementPreview, ManagementTask
from app.modules.reporting.health import _rescue_queued_actions


def test_health_rescue_preserves_payload_actor_terminal_and_bc_scope(
    session, management_env
):
    context, bc, _, route, selection = management_env
    now = datetime.now(UTC)
    preview = ManagementPreview(
        tenant_id=context.tenant_id,
        bc_id=bc.bc_id,
        actor_id=context.actor_id,
        selection_id=selection.id,
        digest="a" * 64,
        route=route.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    session.add(preview)
    session.flush()
    tasks = []
    for status, age in [("QUEUED", 5), ("QUEUED", 1), ("SUCCEEDED", 5)]:
        task = ManagementTask(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            preview_id=preview.id,
            idempotency_key=uuid4(),
            digest="a" * 64,
            status=status,
            created_at=now - timedelta(minutes=age),
            route=route.model_dump(mode="json"),
        )
        session.add(task)
        session.flush()
        tasks.append(task)
        session.add(
            PendingDispatch(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                task_name="ad_management.execute",
                task_key=f"original:{task.id}",
                payload={"task_id": str(task.id)},
                published_at=now - timedelta(minutes=age),
            )
        )
    session.flush()
    plan = SimpleNamespace(tenant_id=context.tenant_id, bc_id=bc.bc_id)
    assert (
        _rescue_queued_actions(
            session,
            SimpleNamespace(tenant_id=context.tenant_id, bc_id="other"),
            now=now,
        )
        == 0
    )
    assert _rescue_queued_actions(session, plan, now=now) == 1
    assert _rescue_queued_actions(session, plan, now=now) == 1
    messages = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_key.startswith("sync-action-repair:")
        )
    ).all()
    assert len(messages) == 1
    assert messages[0].payload == {"task_id": str(tasks[0].id)}
    assert messages[0].actor_id == context.actor_id
    assert [task.status for task in tasks] == ["QUEUED", "QUEUED", "SUCCEEDED"]


def test_no_queued_actions_does_not_scan_historical_dispatches(session, management_env):
    from sqlalchemy import event

    context, bc, *_ = management_env
    statements = []
    connection = session.connection()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert (
            _rescue_queued_actions(
                session,
                SimpleNamespace(tenant_id=context.tenant_id, bc_id=bc.bc_id),
                now=datetime.now(UTC),
            )
            == 0
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    # 空业务队列无需访问历史消息；历史总量不能拖慢分钟级健康检测。
    assert not any("pending_dispatch" in statement for statement in statements)
