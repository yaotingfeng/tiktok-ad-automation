"""规则列表概览与批量操作。"""

from datetime import timedelta

from sqlmodel import select

from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleRun,
)
from app.modules.automation_rules.service import batch_update, overview
from app.modules.tenants.models import TenantMembership
from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_inputs import NOW
from tests.modules.automation_rules.test_runner import make_rule


def _run(session, rule, *, started, summary, status="SUBMITTED"):
    run = AutomationRuleRun(
        tenant_id=rule.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition=rule.definition,
        slot_at=started,
        started_at=started,
        status="COMPLETE",
        mode="AUTO",
        execution_status=status,
        execution_summary=summary,
    )
    session.add(run)
    session.flush()
    return run


def test_overview_counts_last_seven_days(session, sync_seed):
    rule = make_rule(session, sync_seed)
    recent = _run(
        session,
        rule,
        started=NOW - timedelta(days=1),
        summary={"failed": 2, "conflicts": 1},
        status="PARTIAL",
    )
    _run(
        session,
        rule,
        started=NOW - timedelta(days=9),
        summary={"failed": 5, "conflicts": 5},
        status="FAILED",
    )
    for action, days in (("PAUSE", 1), ("PAUSE", 2), ("BUDGET_UP", 3), ("ENABLE", 8)):
        session.add(
            AutomationActionLedger(
                tenant_id=rule.tenant_id,
                rule_id=rule.id,
                run_id=recent.id,
                target_key=f"adgroup:a:{action}{days}",
                action=action,
                created_at=NOW - timedelta(days=days),
            )
        )
    session.add(
        AutomationConfirmation(
            tenant_id=rule.tenant_id, rule_id=rule.id, run_id=recent.id, created_at=NOW
        )
    )
    session.flush()
    result = overview(session, context=sync_seed.context, now=NOW)
    assert result.actions_7d == {"PAUSE": 2, "ENABLE": 0, "BUDGET": 1, "ROAS": 0}
    assert result.pending_confirmations == 1
    assert result.failed_7d == 2 and result.conflicts_7d == 1


def test_batch_update_skips_foreign_and_forbidden(session, sync_seed, other_context):
    mine = make_rule(session, sync_seed)
    auto = make_rule(session, sync_seed, name="auto", mode="AUTO")
    foreign = AutomationRule(
        tenant_id=other_context.tenant_id,
        name="theirs",
        created_by=other_context.actor_id,
        updated_by=other_context.actor_id,
        definition=mine.definition,
    )
    session.add(foreign)
    session.flush()
    result = batch_update(
        session,
        context=sync_seed.context,
        rule_ids=[mine.id, auto.id, foreign.id],
        operation="DISABLE",
    )
    assert result.updated == 1 and not mine.enabled and auto.enabled
    assert {(item.rule_id, item.reason) for item in result.skipped} == {
        (auto.id, "action_forbidden"),
        (foreign.id, "automation_rule_not_found"),
    }
    assert foreign.enabled


def test_batch_delete_over_http(client, session, sync_seed):
    session.get(
        TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)
    ).role = "tenant_admin"
    a = make_rule(session, sync_seed)
    b = make_rule(session, sync_seed, name="b", mode="AUTO")
    session.flush()
    response = client.post(
        f"/api/tenants/{sync_seed.tenant_id}/automation-rules/batch",
        headers=headers(sync_seed.context),
        json={"rule_ids": [str(a.id), str(b.id)], "operation": "DELETE"},
    )
    assert response.status_code == 200 and response.json()["updated"] == 2
    assert (
        session.exec(
            select(AutomationRule).where(
                AutomationRule.tenant_id == sync_seed.tenant_id
            )
        ).all()
        == []
    )
    overview_response = client.get(
        f"/api/tenants/{sync_seed.tenant_id}/automation-rules/overview",
        headers=headers(sync_seed.context),
    )
    assert overview_response.status_code == 200
