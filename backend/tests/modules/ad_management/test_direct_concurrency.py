"""真实 PostgreSQL 多连接验证直接受理/准备围栏；无平台请求。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import TikTokConnection
from app.modules.ad_management.actions import get_task
from app.modules.ad_management.direct_submission import submit_direct_task
from app.modules.ad_management.models import ManagementTask, ManagementTaskItem
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ad_management.schemas import MutationSpec
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _row, _set_selection
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


def test_duplicate_submission_and_preparation_are_serialized(committed_management_env):
    context, bc, account, route, original_selection = committed_management_env
    with Session(engine) as session:
        selection = session.get(FrozenSelectionRecord, original_selection.id)
        refs = [
            EntityRef(
                context.tenant_id, account.advertiser_id, "campaign", f"parallel-{i}"
            )
            for i in range(401)
        ]
        session.add_all(
            [
                _row(
                    ref,
                    connection_id=route.connection_id,
                    configuration={"budget": "100"},
                )
                for ref in refs
            ]
        )
        _set_selection(selection, refs)
        session.add(selection)
        session.commit()
    key, barrier = uuid4(), Barrier(2)

    def submit(_):
        barrier.wait(timeout=5)
        with Session(engine) as session:
            result = submit_direct_task(
                session,
                context,
                bc.bc_id,
                original_selection.id,
                MutationSpec(field="budget", mode="increase_percent", value="10"),
                key,
            )
            session.commit()
            return result.task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_ids = list(pool.map(submit, range(2)))
    assert task_ids[0] == task_ids[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: prepare_direct_task(engine, task_ids[0]), range(2)))
    with Session(engine) as session:
        task = session.get(ManagementTask, task_ids[0])
        items = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task.id)
        ).all()
        assert task.status == "QUEUED" and task.prepare_cursor == 401
        assert len(items) == 401
        assert all(item.final_value == "110.0" for item in items)


def test_preparation_route_revocation_persists_failure(committed_management_env):
    context, bc, _, route, selection = committed_management_env
    with Session(engine) as session:
        result = submit_direct_task(
            session,
            context,
            bc.bc_id,
            selection.id,
            MutationSpec(field="status", mode="set", value="DISABLE"),
            uuid4(),
        )
        connection = session.get(TikTokConnection, route.connection_id)
        connection.status = "DISABLED"
        session.add(connection)
        session.commit()
    prepare_direct_task(engine, result.task_id)
    with Session(engine) as session:
        task = session.get(ManagementTask, result.task_id)
        assert task.status == "FAILED"
        assert task.request["preparation_error"]
        assert "后台准备未完成" in task.request["preparation_error_message"]
        # 恢复同一代数的读取权限后，任务详情显示可操作的中文原因。
        connection = session.get(TikTokConnection, route.connection_id)
        connection.status = "ACTIVE"
        session.add(connection)
        session.flush()
        public = get_task(session, context, bc.bc_id, task.id)
        assert "后台准备未完成" in public.preparation_error
        session.commit()


def test_preparation_checkpoint_survives_database_failure_with_future_dispatch(
    committed_management_env,
):
    """第二块读库失败前，第一块值/游标与恢复消息已经一起提交。"""
    from datetime import UTC, datetime

    import pytest
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from app.jobs.models import PendingDispatch
    from app.modules.ads.models import AdObject

    context, bc, account, route, original_selection = committed_management_env
    with Session(engine) as session:
        selection = session.get(FrozenSelectionRecord, original_selection.id)
        refs = [
            EntityRef(
                context.tenant_id, account.advertiser_id, "campaign", f"fault-{i}"
            )
            for i in range(201)
        ]
        session.add_all(
            [
                _row(
                    ref,
                    connection_id=route.connection_id,
                    configuration={"budget": "100"},
                )
                for ref in refs
            ]
        )
        _set_selection(selection, refs)
        session.add(selection)
        session.flush()
        result = submit_direct_task(
            session,
            context,
            bc.bc_id,
            selection.id,
            MutationSpec(field="budget", mode="increase_percent", value="10"),
            uuid4(),
        )
        session.commit()

    committed_chunks = 0

    def checkpoint_committed(_session):
        nonlocal committed_chunks
        committed_chunks += 1

    def fail_second_directory_read(_conn, _cursor, statement, _parameters, _ctx, _many):
        if committed_chunks >= 1 and "FROM ad_object " in statement:
            raise OperationalError(
                "synthetic directory read",
                {},
                RuntimeError("synthetic connection loss"),
            )

    event.listen(Session, "after_commit", checkpoint_committed)
    event.listen(engine, "before_cursor_execute", fail_second_directory_read)
    try:
        with pytest.raises(OperationalError):
            prepare_direct_task(engine, result.task_id)
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_directory_read)
        event.remove(Session, "after_commit", checkpoint_committed)
    with Session(engine) as session:
        task = session.get(ManagementTask, result.task_id)
        assert task.status == "PREPARING" and task.prepare_cursor == 200
        watches = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_key.startswith(
                    f"ad-management-prepare-watch:{task.id}:"
                ),
            )
        ).all()
        assert watches and all(watch.published_at is None for watch in watches)
        assert all(watch.available_at > datetime.now(UTC) for watch in watches)
        assert len(watches) <= 2  # 同一时间桶内的各块合并为一条恢复消息。
        # 即使原选择已清理、目录原值变化，恢复也不得重算已提交的200项。
        session.delete(session.get(FrozenSelectionRecord, original_selection.id))
        row = session.get(
            AdObject, (context.tenant_id, account.advertiser_id, "campaign", "fault-0")
        )
        row.configuration = {"budget": "999"}
        session.add(row)
        session.commit()
    prepare_direct_task(engine, result.task_id)
    with Session(engine) as session:
        task = session.get(ManagementTask, result.task_id)
        items = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task.id)
        ).all()
        assert task.status == "QUEUED" and task.prepare_cursor == 201
        assert len(items) == 201 and all(item.final_value == "110.0" for item in items)
