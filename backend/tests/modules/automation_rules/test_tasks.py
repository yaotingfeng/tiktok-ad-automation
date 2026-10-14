# test_tasks.py
from datetime import timedelta

from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import (
    AutomationRule,
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


def test_paused_tenant_does_not_starve_others(session, sync_seed, monkeypatch):
    from app.modules.automation_rules import tasks
    from tests.modules.conftest import create_context

    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    monkeypatch.setattr(tasks, "SCAN_LIMIT", 1)
    paused = create_context(session)
    session.add(AutomationTenantSetting(tenant_id=paused.tenant_id, paused=True))
    old = AutomationRule(
        tenant_id=paused.tenant_id,
        name="p",
        created_by=paused.actor_id,
        updated_by=paused.actor_id,
        definition=make_rule(session, sync_seed).definition,
        next_run_at=NOW - timedelta(hours=5),
    )
    session.add(old)
    other = session.exec(
        select(AutomationRule).where(AutomationRule.tenant_id == sync_seed.tenant_id)
    ).one()
    other.next_run_at = NOW - timedelta(minutes=1)
    session.flush()
    assert scan_due_rules(session, now=NOW) == 1
    assert other.next_run_at > NOW


def test_invalid_definition_is_disabled_without_blocking(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    bad = make_rule(session, sync_seed)
    bad.name, bad.definition = "bad", {**bad.definition, "conditions": []}
    bad.next_run_at = NOW - timedelta(hours=1)
    good = make_rule(session, sync_seed)
    good.name, good.next_run_at = "good", NOW - timedelta(minutes=1)
    session.flush()
    assert scan_due_rules(session, now=NOW) == 1
    assert bad.enabled is False and bad.next_run_at is None


def test_runnable_checks_flag_enabled_and_pause(session, sync_seed, monkeypatch):
    from app.modules.automation_rules.tasks import runnable

    rule = make_rule(session, sync_seed)
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", False)
    assert runnable(session, rule) is False
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    assert runnable(session, rule) is True
    rule.enabled = False
    assert runnable(session, rule) is False
    rule.enabled = True
    session.add(AutomationTenantSetting(tenant_id=rule.tenant_id, paused=True))
    session.flush()
    assert runnable(session, rule) is False


def test_execute_task_payload_validation():
    import pytest

    from app.core.errors import DomainError
    from app.modules.automation_rules.tasks import _payload

    with pytest.raises(DomainError):
        _payload(
            {"run_id": "x", "mode": "AUTO", "extra": 1},
            {"run_id", "mode"},
            optional={"selected_keys"},
        )
    assert _payload(
        {"run_id": "x", "mode": "CONFIRM", "selected_keys": ["a"]},
        {"run_id", "mode"},
        optional={"selected_keys"},
    )["selected_keys"] == ["a"]
