"""已创建对象首次列表尚不可见：真实PG/Redis、官方GET边界和持久调度。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.jobs.models import PendingDispatch
from app.modules.builds.dispatch import (
    READ_TASK,
    queue_step,
    repair_execution,
    wake_unit,
)
from app.modules.builds.execution_models import ExecutionStep, Submission
from app.modules.builds.tasks import finish_delivery
from tests.modules.builds.test_reconciliation import arm, run, wire
from tests.modules.builds.test_reconciliation import recon_env as recon_env


def enqueue_readback(env, identity):
    with Session(env.engine) as db, db.begin():
        step = db.get(ExecutionStep, identity)
        submission = db.get(Submission, step.submission_id)
        queue_step(db, step=step, submission=submission, reconcile=True)
        return step.dispatch_revision


def make_due(env, identity):
    with Session(env.engine) as db, db.begin():
        step = db.get(ExecutionStep, identity)
        step.due_at = datetime.now(UTC) - timedelta(seconds=1)
        db.get(PendingDispatch, step.dispatch_id).available_at = step.due_at
        return step.dispatch_revision


def finish(env, identity, revision, result):
    finish_delivery(
        database_engine=env.engine,
        context=env.context,
        step_id=identity,
        revision=revision,
        read_result=result,
    )


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("CAMPAIGN", "campaign_id"),
        ("ADGROUP", "adgroup_id"),
        ("AD", "smart_plus_ad_id"),
    ],
)
def test_known_readback_empty_then_visible_uses_delayed_get_only(
    recon_env, monkeypatch, kind, id_key
):
    env = recon_env
    identity, body = arm(env, kind, known="created-object", readback=True)
    revision = enqueue_readback(env, identity)
    visible = []
    calls = wire(monkeypatch, visible)
    before = datetime.now(UTC)
    result = run(env, identity, revision)
    assert (result.state, result.needs_more, result.retry_after_seconds) == (
        "PENDING",
        True,
        15,
    )
    # 相同投递重跑不读取、不重复消耗预算；完成投递也可安全重放。
    assert run(env, identity, revision) == result
    assert len(calls) == 1
    finish(env, identity, revision, result)
    finish(env, identity, revision, result)
    with Session(env.engine) as db:
        step = db.get(ExecutionStep, identity)
        source = db.get(ExecutionStep, step.parent_step_id)
        dispatch = db.get(PendingDispatch, step.dispatch_id)
        assert step.dispatch_revision == revision + 1
        assert (
            before + timedelta(seconds=15)
            <= step.due_at
            <= datetime.now(UTC) + timedelta(seconds=15)
        )
        assert dispatch.available_at == step.due_at
        assert dispatch.task_name == READ_TASK
        assert source.status == "SUCCEEDED" and source.remote_id == "created-object"
        assert source.request_body == body
        next_revision = step.dispatch_revision
    assert run(env, identity, next_revision).state == "PENDING"
    assert len(calls) == 1
    visible.append({**body, id_key: "created-object"})
    revision = make_due(env, identity)
    result = run(env, identity, revision)
    assert result.state == "SUCCEEDED" and not result.needs_more
    finish(env, identity, revision, result)
    assert len(calls) == 2
    assert all(
        call[1]["filtering"][id_key + "s"] == ["created-object"] for call in calls
    )


def test_empty_readback_delays_survive_wake_repair_and_exhaust_persistently(
    recon_env, monkeypatch
):
    env = recon_env
    identity, body = arm(env, known="created-object", readback=True)
    unknown_ad, _ = arm(env, "AD")
    with Session(env.engine) as db, db.begin():
        original = db.get(ExecutionStep, unknown_ad)
        original.resolved = {**original.resolved, "dispatch_reconciliation_done": True}
        db.flush()
        unknown_before = original.model_dump()
    revision = enqueue_readback(env, identity)
    calls = wire(monkeypatch, [])
    for delay in (15, 30, 60):
        result = run(env, identity, revision)
        with Session(env.engine) as db:
            current = db.get(ExecutionStep, identity)
            assert result.needs_more and result.retry_after_seconds == delay, (
                result,
                current.error_code,
                current.resolved.get("readback_empty_retry"),
                len(calls),
            )
        finish(env, identity, revision, result)
        with Session(env.engine) as db, db.begin():
            step = db.get(ExecutionStep, identity)
            due, dispatch_id = step.due_at, step.dispatch_id
            step.updated_at = datetime.now(UTC) - timedelta(minutes=5)
            wake_unit(db, unit_id=step.unit_id, context=env.context)
        repair_execution(database_engine=env.engine)
        with Session(env.engine) as db:
            step = db.get(ExecutionStep, identity)
            assert step.due_at == due and step.dispatch_id == dispatch_id
            assert db.get(PendingDispatch, dispatch_id).available_at == due
        revision = make_due(env, identity)
    result = run(env, identity, revision)
    assert (result.state, result.needs_more) == ("UNKNOWN", False)
    finish(env, identity, revision, result)
    assert len(calls) == 4
    with Session(env.engine) as db:
        step = db.get(ExecutionStep, identity)
        source = db.get(ExecutionStep, step.parent_step_id)
        assert step.error_code == "readback_inconclusive"
        assert step.dispatch_id is None
        assert step.resolved["dispatch_reconciliation_done"] is True
        assert source.status == "SUCCEEDED" and source.remote_id == "created-object"
        assert source.request_body == body
        assert db.get(ExecutionStep, unknown_ad).model_dump() == unknown_before
    # 新会话、显式重新排同一已知身份也不能暗中重置自动预算。
    revision = enqueue_readback(env, identity)
    assert not run(env, identity, revision).needs_more
    assert len(calls) == 5


def test_unknown_ad_empty_read_preserves_original_terminal_behavior(
    recon_env, monkeypatch
):
    identity, body = arm(recon_env, "AD")
    calls = wire(monkeypatch, [])
    result = run(recon_env, identity)
    assert (result.state, result.needs_more) == ("UNKNOWN", False)
    assert len(calls) == 1
    with Session(recon_env.engine) as db:
        step = db.get(ExecutionStep, identity)
        assert step.status == "UNKNOWN" and step.remote_id is None
        assert step.request_body == body


def test_missing_fields_are_not_retried_as_empty_visibility(recon_env, monkeypatch):
    identity, body = arm(recon_env, known="created-object", readback=True)
    row = {**body, "campaign_id": "created-object"}
    del row["operation_status"]
    calls = wire(monkeypatch, [row])
    result = run(recon_env, identity)
    assert result.state == "UNKNOWN" and not result.needs_more
    assert len(calls) == 1


def test_receipt_id_changed_during_empty_read_does_not_retry(recon_env, monkeypatch):
    env = recon_env
    identity, _ = arm(env, known="created-object", readback=True)

    def changed_receipt(_path, _query):
        with Session(env.engine) as db, db.begin():
            db.get(ExecutionStep, env.ids["CAMPAIGN"]).remote_id = "different-object"

    calls = wire(monkeypatch, [], hook=changed_receipt)
    result = run(env, identity)
    assert result.state == "UNKNOWN" and not result.needs_more
    assert len(calls) == 1
    with Session(env.engine) as db:
        assert "readback_empty_retry" not in db.get(ExecutionStep, identity).resolved


def test_malformed_empty_page_keeps_existing_error_recovery(recon_env, monkeypatch):
    identity, _ = arm(recon_env, known="created-object", readback=True)
    calls = wire(
        monkeypatch,
        [],
        page_transform=lambda data: data["page_info"].update(
            total_number=1, total_page=1
        ),
    )
    result = run(recon_env, identity)
    # 畸形分页仍是原READBACK_ERROR路径，不能被新空结果策略当成正常索引延迟。
    assert (result.state, result.retry_after_seconds) == ("UNKNOWN", 30)
    assert len(calls) == 1
    with Session(recon_env.engine) as db:
        step = db.get(ExecutionStep, identity)
        assert step.error_code == "readback_response_unknown"
        assert "readback_empty_retry" not in step.resolved
