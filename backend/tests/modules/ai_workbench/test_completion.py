"""交付回归：真实持久化边界与异步执行阶段，不调用外部平台。"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ai_workbench import plans_service
from app.modules.ai_workbench.build_handoff import BuildHandoff, _scheduled_at
from app.modules.ai_workbench.models import BuildOccurrence
from app.modules.ai_workbench.plan_schemas import DailyPlanRequest
from app.modules.ai_workbench.plans import AIDailyPlanRow

from .test_daily_plans import _plan_body, _provider_application
from .test_optimizer import api_headers, create_body, seed_optimizer_dependencies


def setup_plan(client, session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = client.post(
        f"/api/tenants/{context.tenant_id}/ai-optimizers",
        headers=api_headers(context),
        json=create_body(strategy.id),
    ).json()
    connection, _ = _provider_application(session, context)
    body = _plan_body(optimizer["id"], connection.id)
    plan = plans_service.create_daily_plan(
        session,
        context=context,
        optimizer_id=UUID(optimizer["id"]),
        body=DailyPlanRequest.model_validate(body),
    )
    return optimizer, plan, body


def test_patch_preserves_row_ids_and_rejects_changes_to_frozen_rows(
    client, session, context
):
    optimizer, plan, body = setup_plan(client, session, context)
    row = plan.rows[0]
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        row.id, uuid4(), enqueue=False
    )
    body["revision"] = plan.revision
    body["rows"][0]["id"] = str(row.id)
    saved = plans_service.update_daily_plan(
        session,
        context=context,
        optimizer_id=UUID(optimizer["id"]),
        body=DailyPlanRequest.model_validate(body),
    )
    assert saved.rows[0].id == row.id
    assert saved.rows[0].read_only is True
    body["revision"] = saved.revision
    body["rows"][0]["title"] = "不能重新搭建的修改"
    with pytest.raises(DomainError) as error:
        plans_service.update_daily_plan(
            session,
            context=context,
            optimizer_id=UUID(optimizer["id"]),
            body=DailyPlanRequest.model_validate(body),
        )
    assert error.value.code == "ai_daily_plan_row_frozen"
    assert session.get(BuildOccurrence, occurrence.id) is not None


def test_scheduler_skips_terminal_heads_and_does_not_freeze_future_rows(
    client, session, context, monkeypatch
):
    from app.modules.ai_workbench.build_tasks import dispatch_due_rows

    optimizer, plan, body = setup_plan(client, session, context)
    row = session.get(AIDailyPlanRow, plan.rows[0].id)
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        row.id, uuid4(), enqueue=False
    )
    occurrence.status = "COMPLETE"
    session.add(occurrence)
    session.flush()
    future = AIDailyPlanRow(
        **{
            **row.model_dump(exclude={"id"}),
            "business_date": date(2099, 1, 1),
            "position": 1,
        }
    )
    session.add(future)
    due = AIDailyPlanRow(
        **{**row.model_dump(exclude={"id"}), "title": "第二剧", "position": 2}
    )
    session.add(due)
    session.flush()
    dispatched = []
    monkeypatch.setattr(
        BuildHandoff, "dispatch_occurrence", lambda self, oid: dispatched.append(oid)
    )
    dispatch_due_rows(session, now=datetime(2026, 11, 12, 20, tzinfo=UTC), limit=1)
    assert not dispatched
    assert (
        session.exec(
            select(BuildOccurrence).where(BuildOccurrence.plan_row_id == future.id)
        ).first()
        is None
    )
    assert (
        session.exec(
            select(BuildOccurrence).where(BuildOccurrence.plan_row_id == due.id)
        ).first()
        is not None
    )


def test_schedule_uses_shanghai_local_time():
    row = AIDailyPlanRow(
        tenant_id=uuid4(),
        optimizer_id=uuid4(),
        plan_id=uuid4(),
        business_date=date(2026, 11, 12),
        scheduled_time=time(9, 30),
        title="剧",
        provider_connection_id=uuid4(),
        application_id="a",
    )
    assert _scheduled_at(row) == datetime(2026, 11, 12, 1, 30, tzinfo=UTC)


def test_async_preparation_waits_before_generating_preview(
    client, session, context, monkeypatch
):
    optimizer, plan, _ = setup_plan(client, session, context)
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        plan.rows[0].id, uuid4(), enqueue=False
    )
    draft_id = uuid4()
    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.drafts.create_draft",
        lambda *a, **kw: draft_id,
    )
    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.drafts.prepare_draft",
        lambda *a, **kw: uuid4(),
    )
    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.drafts.get_draft",
        lambda *a, **kw: type("Draft", (), {"status": "PREPARING", "revision": 1})(),
    )

    def preview(*_args, **_kwargs):
        raise AssertionError("未就绪时不能生成预览")

    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.previews.generate_preview", preview
    )
    result = BuildHandoff(session, context=context).dispatch_occurrence(occurrence.id)
    assert result.status == "WAITING_MATERIAL"
    assert result.error_code != "draft_not_ready"


def test_already_queued_occurrences_do_not_starve_next_rows(client, session, context):
    from app.modules.ai_workbench.build_tasks import dispatch_due_rows

    _, plan, _ = setup_plan(client, session, context)
    first = session.get(AIDailyPlanRow, plan.rows[0].id)
    BuildHandoff(session, context=context).create_occurrence(first.id, uuid4())
    second = AIDailyPlanRow(
        **{
            **first.model_dump(exclude={"id"}),
            "title": "排队之后的剧",
            "scheduled_time": time(9, 30),
            "position": 1,
        }
    )
    session.add(second)
    session.flush()
    dispatch_due_rows(session, now=datetime(2026, 11, 12, 10, tzinfo=UTC), limit=1)
    assert (
        session.exec(
            select(BuildOccurrence).where(BuildOccurrence.plan_row_id == second.id)
        ).first()
        is not None
    )


def test_pausing_optimizer_stops_already_queued_occurrence(
    client, session, context, monkeypatch
):
    from app.modules.ai_workbench.models import AIOptimizer

    optimizer, plan, _ = setup_plan(client, session, context)
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        plan.rows[0].id, uuid4(), enqueue=False
    )
    saved = session.get(AIOptimizer, UUID(optimizer["id"]))
    saved.active = False
    session.add(saved)
    session.flush()

    def create(*_args, **_kwargs):
        raise AssertionError("已暂停优化师不能创建搭建草稿")

    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.drafts.create_draft", create
    )
    assert (
        BuildHandoff(session, context=context)
        .dispatch_occurrence(occurrence.id)
        .error_code
        == "ai_optimizer_inactive"
    )


def test_ai_metadata_matches_migrated_postgresql_schema(session):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    comparison = MigrationContext.configure(
        session.connection(),
        opts={
            "include_object": lambda _obj, name, kind, _reflected, _compare: (
                name.startswith("ai_") if kind == "table" else True
            ),
        },
    )
    assert compare_metadata(comparison, SQLModel.metadata) == []


def test_future_and_expired_dispatch_never_prepare_ads(
    client, session, context, monkeypatch
):
    from app.modules.ai_workbench.build_handoff import _scheduled_at

    _, plan, _ = setup_plan(client, session, context)
    row = session.get(AIDailyPlanRow, plan.rows[0].id)
    row.business_date = date(2099, 1, 1)
    session.add(row)
    session.flush()
    occurrence = BuildHandoff(session, context=context).create_occurrence(
        row.id, uuid4(), enqueue=False
    )

    def prepare(*_a, **_kw):
        raise AssertionError("未到期或已过期不应准备搭建")

    monkeypatch.setattr(
        "app.modules.ai_workbench.build_handoff.drafts.create_draft", prepare
    )
    result = BuildHandoff(session, context=context).dispatch_occurrence(occurrence.id)
    assert result.status == "WAITING_MATERIAL"
    assert result.error_code == "ai_schedule_not_due"
    row.business_date = date(2000, 1, 1)
    occurrence.scheduled_at = _scheduled_at(row)
    session.add_all([row, occurrence])
    session.flush()
    assert (
        BuildHandoff(session, context=context)
        .dispatch_occurrence(occurrence.id)
        .error_code
        == "schedule_expired"
    )


def test_blocked_oldest_row_yields_to_next_tenant_work(
    client, session, context, monkeypatch
):
    from datetime import timedelta

    from app.modules.ai_workbench.build_tasks import dispatch_due_rows

    _, plan, _ = setup_plan(client, session, context)
    first = session.get(AIDailyPlanRow, plan.rows[0].id)
    second = AIDailyPlanRow(
        **{
            **first.model_dump(exclude={"id"}),
            "title": "未受阻排期",
            "scheduled_time": time(9, 30),
            "position": 1,
        }
    )
    session.add(second)
    session.flush()
    original = BuildHandoff.create_occurrence

    def create(handoff, row_id, *args, **kwargs):
        if row_id == first.id:
            raise DomainError("ai_provider_application_not_found", "受阻测试")
        return original(handoff, row_id, *args, **kwargs)

    monkeypatch.setattr(BuildHandoff, "create_occurrence", create)
    now = datetime(2026, 11, 12, 10, tzinfo=UTC)
    dispatch_due_rows(session, now=now, limit=1)
    dispatch_due_rows(session, now=now + timedelta(seconds=30), limit=1)
    assert (
        session.exec(
            select(BuildOccurrence).where(BuildOccurrence.plan_row_id == second.id)
        ).first()
        is not None
    )
