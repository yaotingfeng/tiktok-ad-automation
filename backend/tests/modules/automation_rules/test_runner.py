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
