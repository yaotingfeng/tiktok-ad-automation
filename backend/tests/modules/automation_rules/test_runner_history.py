from decimal import Decimal

from sqlalchemy import text

from app.modules.ai_workbench.rule_history import rule_snapshot_report
from app.modules.automation_rules.models import AutomationRuleEvidence
from app.modules.automation_rules.runner import execute_run
from app.modules.automation_rules.schemas import RuleDefinition
from tests.modules.automation_rules.test_inputs import NOW, _seed
from tests.modules.automation_rules.test_runner import make_rule


def test_rule_without_build_freezes_nonhit_metrics_before_reporting_changes(
    session, sync_seed
):
    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.status == "COMPLETE"
    row = session.get(AutomationRuleEvidence, run.evidence_id)
    assert row is not None
    session.flush()
    session.expire(row)
    before = rule_snapshot_report(row)
    assert before["replay"]["current"]["hit_subjects"] == ["adgroup:acc-1:g1"]
    assert Decimal(row.checked_metrics["TODAY"]["adgroup:acc-1:g1"]["spend"]) == 10
    assert Decimal(row.checked_metrics["TODAY"]["adgroup:acc-2:g9"]["spend"]) == 2
    session.execute(
        text("UPDATE ad_report_day SET spend=999 WHERE tenant_id=:tenant"),
        {"tenant": sync_seed.tenant_id},
    )
    assert rule_snapshot_report(row) == before
    candidate = dict(row.definition)
    candidate["conditions"] = [
        {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]}
    ]
    report = rule_snapshot_report(
        row, candidate_definition=RuleDefinition.model_validate(candidate)
    )
    assert report["replay"]["candidate"]["hit_subjects"] == [
        "adgroup:acc-1:g1",
        "adgroup:acc-2:g9",
    ]
