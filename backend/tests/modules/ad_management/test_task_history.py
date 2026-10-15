"""操作记录列表：按时间倒序、动作与来源筛选，以及摘要字段。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.errors import DomainError
from app.models import User
from app.modules.ad_management.actions import get_task, list_tasks
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.automation_rules.models import AutomationActionLedger, AutomationRule

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


@pytest.fixture
def history(session: Session, management_env):
    context, bc, account, route, selection = management_env
    # 可见性校验只看任务项冻结的能力；列表测试统一用预算能力，动作看预览变更。
    session.flush()

    def add(mutation: dict, *, minutes: int, kinds=("adgroup",)) -> ManagementTask:
        created = NOW + timedelta(minutes=minutes)
        preview = ManagementPreview(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            selection_id=selection.id,
            digest=uuid4().hex * 2,
            mutation=mutation,
            route=route.model_dump(mode="json"),
            created_at=created,
            expires_at=created + timedelta(minutes=5),
            status="READY",
            counts={"selected": len(kinds), "targets": len(kinds)},
        )
        session.add(preview)
        session.flush()
        task = ManagementTask(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            preview_id=preview.id,
            idempotency_key=uuid4(),
            digest=preview.digest,
            route=route.model_dump(mode="json"),
            status="SUCCEEDED",
            counts={"targets": len(kinds), "accepted": len(kinds)},
            created_at=created,
            updated_at=created,
        )
        session.add(task)
        session.flush()
        for position, kind in enumerate(kinds):
            ref = {
                "tenant_id": str(context.tenant_id),
                "advertiser_id": account.advertiser_id,
                "kind": kind,
                "remote_id": f"{task.id.hex[:8]}-{position}",
            }
            capability = {
                "operation": "update_budget",
                "entity_kind": "adgroup",
                "route": route.model_dump(mode="json"),
            }
            preview_item = ManagementPreviewItem(
                tenant_id=context.tenant_id,
                preview_id=preview.id,
                position=position,
                ref=ref,
                execution_result="PENDING",
                capability=capability,
            )
            session.add(preview_item)
            session.flush()
            session.add(
                ManagementTaskItem(
                    tenant_id=context.tenant_id,
                    task_id=task.id,
                    preview_item_id=preview_item.id,
                    preview_id=preview.id,
                    position=position,
                    ref=ref,
                    execution_result="ACCEPTED",
                    capability=capability,
                )
            )
        session.flush()
        return task

    return context, bc, add


def _rule_ledger(session: Session, context, task: ManagementTask, name: str):
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name=name,
        definition={},
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(rule)
    session.flush()
    session.add(
        AutomationActionLedger(
            tenant_id=context.tenant_id,
            rule_id=rule.id,
            target_key="target",
            action="budget",
            management_task_id=task.id,
        )
    )
    session.flush()
    return rule


def test_list_is_newest_first_with_stable_cursor(session, history):
    context, bc, add = history
    tasks = [
        add({"field": "budget", "mode": "set", "value": "10"}, minutes=minute)
        for minute in (5, 1, 9, 3)
    ]
    first = list_tasks(session, context, bc.bc_id, cursor=None, limit=2)
    second = list_tasks(session, context, bc.bc_id, cursor=first.next_cursor, limit=2)
    by_time = sorted(tasks, key=lambda task: task.created_at, reverse=True)
    assert [item.task_id for item in first.items + second.items] == [
        task.id for task in by_time
    ]
    assert first.total == 4 and second.next_cursor is None
    with pytest.raises(DomainError):
        list_tasks(session, context, bc.bc_id, cursor="not-a-cursor", limit=2)


def test_summary_carries_time_actor_action_source_and_levels(session, history):
    context, bc, add = history
    actor = session.get(User, context.actor_id)
    assert actor is not None
    actor.full_name = "投放同学"
    session.add(actor)
    manual = add(
        {"field": "budget", "mode": "increase_percent", "value": "20"},
        minutes=1,
        kinds=("adgroup", "adgroup", "campaign"),
    )
    by_rule = add({"field": "status", "mode": "set", "value": "DISABLE"}, minutes=2)
    rule = _rule_ledger(session, context, by_rule, "夜间暂停")

    page = list_tasks(session, context, bc.bc_id, cursor=None, limit=10)
    rows = {item.task_id: item for item in page.items}
    assert rows[manual.id].created_at == manual.created_at
    assert rows[manual.id].actor_name == "投放同学"
    assert rows[manual.id].mutation is not None
    assert rows[manual.id].mutation.mode == "increase_percent"
    assert str(rows[manual.id].mutation.value) == "20"
    assert rows[manual.id].target_kinds == {"adgroup": 2, "campaign": 1}
    assert rows[manual.id].source is not None
    assert rows[manual.id].source.kind == "manual"
    assert rows[by_rule.id].source is not None
    assert rows[by_rule.id].source.kind == "rule"
    assert rows[by_rule.id].source.rule_name == "夜间暂停"
    assert rows[by_rule.id].source.rule_id == rule.id
    # 详情与列表使用同一份摘要。
    detail = get_task(session, context, bc.bc_id, by_rule.id)
    assert detail.source == rows[by_rule.id].source
    assert detail.items

    # 规则删除后台账的 rule_id 置空，来源仍标记为规则。
    session.delete(rule)
    session.flush()
    session.expire_all()
    again = list_tasks(session, context, bc.bc_id, cursor=None, limit=10)
    source = {item.task_id: item.source for item in again.items}[by_rule.id]
    assert source is not None and source.kind == "rule" and source.rule_id is None


def test_filters_by_action_and_source(session, history):
    context, bc, add = history
    budget = add({"field": "budget", "mode": "set", "value": "10"}, minutes=1)
    enable = add({"field": "status", "mode": "set", "value": "ENABLE"}, minutes=2)
    disable = add({"field": "status", "mode": "set", "value": "DISABLE"}, minutes=3)
    roas = add({"field": "roas", "mode": "set", "value": "1.2"}, minutes=4)
    _rule_ledger(session, context, disable, "夜间暂停")

    def ids(**filters):
        page = list_tasks(session, context, bc.bc_id, cursor=None, limit=10, **filters)
        return {item.task_id for item in page.items}

    assert ids(action="budget") == {budget.id}
    assert ids(action="enable") == {enable.id}
    assert ids(action="disable") == {disable.id}
    assert ids(action="roas") == {roas.id}
    assert ids(source="rule") == {disable.id}
    assert ids(source="manual") == {budget.id, enable.id, roas.id}
    assert ids(action="disable", source="manual") == set()
    assert (
        list_tasks(
            session, context, bc.bc_id, cursor=None, limit=10, source="rule"
        ).total
        == 1
    )


def test_filter_rename_tasks(session, history):
    context, bc, add = history
    add({"field": "budget", "mode": "set", "value": "10"}, minutes=1)
    rename = add(
        {
            "field": "name",
            "mode": "set",
            "value": None,
            "rename": {"kind": "affix", "prefix": "嘉书-"},
        },
        minutes=2,
        kinds=("campaign",),
    )
    page = list_tasks(
        session, context, bc.bc_id, cursor=None, limit=10, action="rename"
    )
    assert [item.task_id for item in page.items] == [rename.id]
    mutation = page.items[0].mutation
    assert mutation is not None
    assert mutation.field == "name"
    assert mutation.rename_kind == "affix"
    assert mutation.value is None
