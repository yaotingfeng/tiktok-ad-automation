# test_tasks.py
from datetime import timedelta

from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import (
    AutomationRuleRun,
    AutomationTenantSetting,
)
from app.modules.automation_rules.tasks import cleanup_runs, scan_due_rules
from tests.modules.automation_rules.test_inputs import NOW
from tests.modules.automation_rules.test_runner import make_rule


def test_scan_respects_flag_pause_and_advances(session, sync_seed, monkeypatch):
    rule = make_rule(session, sync_seed)
    rule.next_run_at = NOW - timedelta(minutes=1)
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", False)
    assert scan_due_rules(session, now=NOW) == 0
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    session.add(AutomationTenantSetting(tenant_id=sync_seed.tenant_id, paused=True))
    session.flush()
    assert scan_due_rules(session, now=NOW) == 0
    session.get(AutomationTenantSetting, sync_seed.tenant_id).paused = False
    assert scan_due_rules(session, now=NOW) == 1
    assert rule.next_run_at > NOW
    queued = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.task_name == "automation.run_rule"
        )
    ).all()
    assert len(queued) == 1
    assert scan_due_rules(session, now=NOW) == 0  # 不重复投递


def test_once_rule_disables_after_scheduling(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    rule = make_rule(
        session,
        sync_seed,
        schedule={
            "frequency": "30M",
            "window": "ONCE",
            "once_at": (NOW - timedelta(minutes=1)).isoformat(),
        },
    )
    rule.next_run_at = NOW - timedelta(minutes=1)
    assert scan_due_rules(session, now=NOW) == 1
    assert rule.enabled is False and rule.next_run_at is None


def test_cleanup_keeps_30_days(session, sync_seed):
    rule = make_rule(session, sync_seed)
    for days in (1, 31):
        session.add(
            AutomationRuleRun(
                tenant_id=rule.tenant_id,
                rule_id=rule.id,
                rule_version=1,
                definition={},
                slot_at=NOW - timedelta(days=days),
                started_at=NOW - timedelta(days=days),
            )
        )
    session.flush()
    assert cleanup_runs(session, now=NOW) == 1
