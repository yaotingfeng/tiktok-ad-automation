# test_runner.py
import httpx
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import (
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.runner import deliver_notification, execute_run
from app.modules.automation_rules.schemas import AutomationSettingsUpdate
from app.modules.automation_rules.settings_service import update_settings
from tests.modules.automation_rules.test_definition import base
from tests.modules.automation_rules.test_inputs import NOW, _seed

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/run-hook"


def make_rule(session, seed, **patch):
    # 种子中 g1 今天消耗 10、g9 为 2;门槛 5 只命中 g1。
    patch.setdefault(
        "conditions",
        [{"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["5"]}],
    )
    rule = AutomationRule(
        tenant_id=seed.tenant_id,
        name="r",
        created_by=seed.actor_id,
        updated_by=seed.actor_id,
        definition=base(**patch),
    )
    session.add(rule)
    session.flush()
    return rule


def test_run_persists_hits_once_per_slot_and_queues_feishu(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    again = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert again.id == run.id and run.status == "COMPLETE"
    hits = session.exec(
        select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)
    ).all()
    assert [h.subject_key for h in hits] == ["adgroup:acc-1:g1"]
    assert run.notify_status == "PENDING"
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "automation.send_feishu"
        )
    ).all()
    assert len(dispatch) == 1 and dispatch[0].payload == {"run_id": str(run.id)}
    assert rule.last_run_at == NOW


def test_no_hits_no_notification_by_default(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(
        session,
        sync_seed,
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["99999"]}
        ],
    )
    assert (
        execute_run(session, rule=rule, slot_at=NOW, now=NOW).notify_status
        == "NOT_REQUIRED"
    )


def test_owner_without_access_fails_closed(session, sync_seed):
    from app.modules.tenants.models import TenantMembership

    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed)
    session.get(
        TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)
    ).active = False
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.status == "FAILED" and run.error_code == "rule_owner_forbidden"


def test_deliver_marks_sent_and_failed(session, sync_seed):
    from app.modules.tenants.models import TenantMembership

    _seed(session, sync_seed)
    session.get(
        TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)
    ).role = "tenant_admin"
    update_settings(
        session,
        context=sync_seed.context,
        body=AutomationSettingsUpdate(
            schedule_timezone="Asia/Shanghai",
            paused=False,
            feishu_enabled=True,
            webhook_url=HOOK,
        ),
    )
    rule = make_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    sent = []
    ok = httpx.MockTransport(
        lambda r: sent.append(r) or httpx.Response(200, json={"code": 0})
    )
    assert deliver_notification(session, run_id=run.id, transport=ok) == "SENT"
    assert "组一" in sent[0].content.decode()
    run.notify_status = "PENDING"
    bad = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 9499, "msg": "bad"})
    )
    assert deliver_notification(session, run_id=run.id, transport=bad) == "FAILED"
    assert "bad" in session.get(AutomationRuleRun, run.id).notify_error


def test_database_error_during_evaluation_records_failed_run(
    session, sync_seed, monkeypatch
):
    from sqlalchemy import text

    from app.modules.automation_rules import runner

    def broken(session, **_kwargs):
        session.connection().execute(text("SELECT 1/0"))

    _seed(session, sync_seed)
    monkeypatch.setattr(runner, "load_inputs", broken)
    run = execute_run(session, rule=make_rule(session, sync_seed), slot_at=NOW, now=NOW)
    session.flush()
    assert run.status == "FAILED" and run.error_code == "evaluation_failed"


def test_long_drama_names_are_truncated(session, sync_seed):
    from app.modules.ads.models import CampaignNameProjection

    _seed(session, sync_seed)
    row = session.get(CampaignNameProjection, (sync_seed.tenant_id, "acc-1", "c1", 1))
    row.drama_name = "长" * 400
    session.flush()
    run = execute_run(
        session,
        rule=make_rule(session, sync_seed, aggregation="DRAMA"),
        slot_at=NOW,
        now=NOW,
    )
    assert run.status == "COMPLETE" and run.hit_count == 1


def _dispatches(session, name):
    return session.exec(
        select(PendingDispatch).where(PendingDispatch.task_name == name)
    ).all()


def test_auto_run_queues_execution(session, sync_seed):
    _seed(session, sync_seed)
    run = execute_run(
        session, rule=make_rule(session, sync_seed, mode="AUTO"), slot_at=NOW, now=NOW
    )
    assert run.mode == "AUTO" and run.execution_status == "QUEUED"
    queued = _dispatches(session, "automation.execute")
    assert [d.payload for d in queued] == [{"run_id": str(run.id), "mode": "AUTO"}]
    # 自动执行的通知在执行完成后由执行任务发送。
    assert run.notify_status == "NOT_REQUIRED" and not _dispatches(
        session, "automation.send_feishu"
    )


def test_confirm_run_opens_batch_and_expires_previous(session, sync_seed):
    from datetime import timedelta

    from app.modules.automation_rules.models import AutomationConfirmation

    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed, mode="CONFIRM")
    first = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert first.execution_status == "AWAITING_CONFIRMATION"
    hit = session.exec(
        select(AutomationRuleHit).where(AutomationRuleHit.run_id == first.id)
    ).one()
    assert hit.outcome == "PENDING_CONFIRMATION"
    later = NOW + timedelta(minutes=30)
    second = execute_run(session, rule=rule, slot_at=later, now=later)
    batches = {
        b.run_id: b.status for b in session.exec(select(AutomationConfirmation)).all()
    }
    assert batches == {first.id: "EXPIRED", second.id: "PENDING"}
    assert first.execution_status == "EXPIRED"
    assert first.notify_status == "PENDING"


def test_auto_over_limit_becomes_confirmation(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(
        session,
        sync_seed,
        mode="AUTO",
        aggregation="ALL",
        max_targets=1,
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]}
        ],
    )
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.execution_status == "AWAITING_CONFIRMATION"
    assert run.execution_summary["reason"] == "max_targets_exceeded"
    assert not _dispatches(session, "automation.execute")
