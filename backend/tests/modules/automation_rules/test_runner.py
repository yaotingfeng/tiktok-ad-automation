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


def make_rule(session, seed, name="r", **patch):
    # 种子中 g1 今天消耗 10、g9 为 2;门槛 5 只命中 g1。
    patch.setdefault(
        "conditions",
        [{"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["5"]}],
    )
    rule = AutomationRule(
        tenant_id=seed.tenant_id,
        name=name,
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


def test_auto_executes_all_hits_without_target_limit(session, sync_seed):
    # 自动执行作用于选定的全部范围，命中多少就执行多少，不转人工确认。
    _seed(session, sync_seed)
    rule = make_rule(
        session,
        sync_seed,
        mode="AUTO",
        aggregation="ALL",
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]}
        ],
    )
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.execution_status == "QUEUED"
    assert len(_dispatches(session, "automation.execute")) == 1


def _card_text(card):
    return "\n".join(
        element["text"]["content"]
        for element in card["card"]["elements"]
        if element["tag"] == "div"
    )


def _button(card):
    action = next(e for e in card["card"]["elements"] if e["tag"] == "action")
    return action["actions"][0]


def test_confirmation_card_links_to_batch(session, sync_seed):
    from app.modules.automation_rules.models import AutomationConfirmation
    from app.modules.automation_rules.runner import build_run_card

    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed, mode="CONFIRM")
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    batch = session.exec(select(AutomationConfirmation)).one()
    hits = session.exec(
        select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)
    ).all()
    card = build_run_card(rule, run, list(hits), confirmation_id=batch.id)
    assert card["card"]["header"]["title"]["content"] == "【待确认】r"
    assert card["card"]["header"]["template"] == "yellow"
    button = _button(card)
    assert button["text"]["content"] == "去确认"
    assert button["url"].endswith(
        f"/automation-rules?tab=confirmations&confirmation={batch.id}"
    )
    assert "下次运行前确认" in _card_text(card)


def test_execution_result_card(session, sync_seed):
    from app.modules.automation_rules.runner import build_run_card

    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed, mode="AUTO")
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    run.execution_status = "PARTIAL"
    run.execution_summary = {
        "submitted": 2,
        "skipped_frequency": 1,
        "conflicts": 1,
        "no_change": 4,
        "failed": 1,
        "failed_bcs": [{"bc_id": "bc-y", "reason": "route_unavailable"}],
        "tasks": [],
        "conflict_details": [
            {"target_key": "adgroup:acc-1:g1", "rule_name": "拉量", "action": "ENABLE"}
        ],
    }
    card = build_run_card(rule, run, [], confirmation_id=None)
    assert card["card"]["header"]["title"]["content"] == "【部分提交】r"
    assert card["card"]["header"]["template"] == "red"
    text = _card_text(card)
    assert "提交 2 个对象" in text
    assert "频控跳过 1、规则冲突 1、无需变更 4" in text
    assert "失败的 BC：1 个；原因：route_unavailable" in text
    assert "规则「拉量」仍命中，已阻止反向操作" in text
    assert "adgroup:acc-1:g1" not in text
    run.execution_status, run.execution_summary["failed_bcs"] = "SUBMITTED", []
    run.execution_summary["failed"] = 0
    card = build_run_card(rule, run, [], confirmation_id=None)
    assert card["card"]["header"]["title"]["content"] == "【已提交】r"
    assert card["card"]["header"]["template"] == "green"
    assert _button(card)["url"].endswith(f"tab=runs&run={run.id}")
    run.execution_status = "FAILED"
    card = build_run_card(rule, run, [], confirmation_id=None)
    assert card["card"]["header"]["title"]["content"] == "【执行失败】r"
    assert card["card"]["header"]["template"] == "red"


def test_execution_result_notification_respects_rule_setting(session, sync_seed):
    from app.modules.automation_rules.runner import notify_execution

    _seed(session, sync_seed)
    quiet = make_rule(session, sync_seed, mode="AUTO", notification={"enabled": False})
    run = execute_run(session, rule=quiet, slot_at=NOW, now=NOW)
    run.execution_status = "SUBMITTED"
    notify_execution(session, run=run, rule=quiet)
    assert run.notify_status == "NOT_REQUIRED"
    quiet.name = "quiet"
    session.flush()
    loud = make_rule(session, sync_seed, mode="AUTO")
    run = execute_run(session, rule=loud, slot_at=NOW, now=NOW)
    run.execution_status = "SUBMITTED"
    notify_execution(session, run=run, rule=loud)
    assert run.notify_status == "PENDING"
    assert [d.task_key for d in _dispatches(session, "automation.send_feishu")] == [
        f"automation-feishu:{run.id}:SUBMITTED"
    ]


def test_skipped_execution_card(session, sync_seed):
    from app.modules.automation_rules.runner import build_run_card

    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed, mode="AUTO")
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    run.execution_status = "SKIPPED"
    run.execution_summary = {
        "submitted": 0,
        "skipped_frequency": 1,
        "unsupported": 2,
        "unsupported_details": [
            {"target_key": "material:a:v1", "reason": "material_status_unsupported"}
        ],
    }
    card = build_run_card(rule, run, [], confirmation_id=None)
    assert "不支持操作 2 个对象" in _card_text(card)
    assert card["card"]["header"]["title"]["content"] == "【未执行】r"
    assert card["card"]["header"]["template"] == "grey"
    assert "频控跳过 1" in _card_text(card)


def test_execution_error_lines():
    from app.modules.automation_rules.runner import _execution_lines

    assert "部署开关已关闭" in "".join(
        _execution_lines({"error": "automation_disabled"})
    )
    assert "执行异常" in "".join(_execution_lines({"error": "execution_error"}))
    assert "已无执行权限" in "".join(
        _execution_lines({"error": "rule_owner_forbidden"})
    )


def test_repeated_stale_accounts_without_hits_not_resent(session, sync_seed):
    from datetime import timedelta

    # acc-2 数据过期且无命中：第一次发卡片，之后同样的过期账户不再重复发送。
    _seed(session, sync_seed, fresh=("acc-1",))
    rule = make_rule(
        session,
        sync_seed,
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["999"]}
        ],
    )
    first = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert first.hit_count == 0 and first.skipped_accounts
    assert first.notify_status == "PENDING"
    first.notify_status = "SENT"
    later = NOW + timedelta(minutes=30)
    second = execute_run(session, rule=rule, slot_at=later, now=later)
    assert second.notify_status == "NOT_REQUIRED"
