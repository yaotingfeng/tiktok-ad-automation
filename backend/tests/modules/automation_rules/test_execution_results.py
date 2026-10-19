"""管理回执完成后，规则记录显示真实结算结果，通知幂等。"""

from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ad_management.models import ManagementTaskItem
from app.modules.automation_rules.executor import execute_hits
from app.modules.automation_rules.runner import execution_brief
from tests.modules.automation_rules.test_executor import NOW, PAUSE, _rule_run, _target
from tests.modules.automation_rules.test_executor import env as env


def test_unknown_receipt_updates_rule_and_does_not_duplicate_notification(session, env):
    from app.modules.automation_rules.results import refresh_execution_results

    rule, run = _rule_run(
        session, env, action=PAUSE, targets=[_target("acc-x", "bc-x")]
    )
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=env.actor_id,
        require_manage=True,
    )
    item = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.tenant_id == env.tenant_id)
    ).one()
    item.execution_result = "UNKNOWN"
    item.reason = "management_result_unknown"
    session.flush()
    refresh_execution_results(session, item.task_id, env.tenant_id)
    assert run.execution_status == "NEEDS_REVIEW"
    assert run.execution_summary["results"]["unknown"] == 1
    assert "结果未知 1" in execution_brief(run.execution_summary, "暂停")
    first = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "automation.send_feishu"
        )
    ).all()
    assert len(first) == 1
    refresh_execution_results(session, item.task_id, env.tenant_id)
    assert len(
        session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "automation.send_feishu"
            )
        ).all()
    ) == len(first)


def test_waits_for_all_bcs_and_deferred_retries_before_final_notification(session, env):
    from app.modules.ad_management.models import ManagementRequestAttempt
    from app.modules.automation_rules.results import refresh_execution_results
    from tests.modules.automation_rules.test_executor import _bc, _objects

    connection = _bc(session, env, "bc-y", "acc-y")
    _objects(session, env, connection, "acc-y")
    rule, run = _rule_run(
        session,
        env,
        action=PAUSE,
        targets=[_target("acc-x", "bc-x"), _target("acc-y", "bc-y")],
    )
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=None,
        now=NOW,
        actor_id=env.actor_id,
        require_manage=True,
    )
    items = session.exec(
        select(ManagementTaskItem).where(ManagementTaskItem.tenant_id == env.tenant_id)
    ).all()
    assert len(items) == 2
    first, second = items
    first.execution_result = "ACCEPTED"
    second.execution_result = "NOT_SENT"
    attempt = ManagementRequestAttempt(
        tenant_id=env.tenant_id,
        task_item_id=second.id,
        outcome="NOT_SENT",
        retryable=True,
    )
    session.add(attempt)
    session.flush()
    refresh_execution_results(session, first.task_id, env.tenant_id)
    assert run.execution_status == "SUBMITTED"
    assert run.execution_summary["results"] == {"accepted": 1, "pending": 1}
    assert (
        session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "automation.send_feishu"
            )
        ).all()
        == []
    )
    attempt.retryable = False
    session.flush()
    refresh_execution_results(session, second.task_id, env.tenant_id)
    assert run.execution_status == "PARTIAL"
    assert run.execution_summary["results"] == {"accepted": 1, "not_sent": 1}
    assert "未发送 1" in execution_brief(run.execution_summary, "暂停")
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.task_name == "automation.send_feishu"
                )
            ).all()
        )
        == 1
    )
