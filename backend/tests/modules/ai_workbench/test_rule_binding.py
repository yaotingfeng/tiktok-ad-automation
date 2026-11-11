from datetime import UTC, datetime
from uuid import uuid4

from app.modules.ai_workbench.evidence import RuleEvidenceSnapshot
from app.modules.ai_workbench.rule_binding import RuleBindingScope, _rule_accounts
from app.modules.automation_rules.evaluate import Evaluation, Hit, Target
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from app.modules.automation_rules.schemas import RuleDefinition
from tests.modules.automation_rules.test_definition import base


def test_effective_scope_is_the_intersection_of_optimizer_and_rule_accounts():
    definition = RuleDefinition.model_validate(
        base(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["a-1", "a-2"]})
    )
    assert _rule_accounts(definition, bc_id="bc-x") == {"a-1", "a-2"}
    # A BC-scoped rule applies to every account in that BC; the optimizer
    # intersection is performed by resolve_rule_binding after this result.
    bc_definition = RuleDefinition.model_validate(
        base(accounts={"mode": "BC", "bc_ids": ["bc-x"]})
    )
    assert _rule_accounts(bc_definition, bc_id="bc-x") is None
    assert _rule_accounts(bc_definition, bc_id="bc-other") == set()


def test_evidence_freezes_rule_version_scope_hits_and_retention():
    tenant_id, rule_id, optimizer_id = uuid4(), uuid4(), uuid4()
    rule = AutomationRule(
        tenant_id=tenant_id,
        id=rule_id,
        name="optimizer-rule",
        created_by=uuid4(),
        updated_by=uuid4(),
        definition=base(
            conditions=[
                {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["5"]}
            ]
        ),
        version=3,
    )
    run = AutomationRuleRun(
        tenant_id=tenant_id,
        rule_id=rule_id,
        optimizer_id=optimizer_id,
        optimizer_version=7,
        binding_version=7,
        rule_version=3,
        slot_at=datetime.now(UTC),
        definition=rule.definition,
    )
    target = Target(
        key="adgroup:a-1:g-1",
        kind="adgroup",
        advertiser_id="a-1",
        bc_id="bc-x",
        remote_id="g-1",
        name="g-1",
    )
    result = Evaluation(
        checked=1,
        skipped_count=1,
        hits=(
            Hit(
                subject_key=target.key,
                subject_label=target.name,
                drama=None,
                advertiser_id="a-1",
                bc_id="bc-x",
                currency="USD",
                metrics={"spend": "10"},
                targets=(target,),
                target_count=1,
            ),
        ),
    )
    snapshot = RuleEvidenceSnapshot.from_evaluation(
        rule=rule,
        run=run,
        result=result,
        skipped_accounts=[{"advertiser_id": "a-2", "reason": "STALE_REPORT"}],
        scope=RuleBindingScope(
            tenant_id=tenant_id,
            optimizer_id=optimizer_id,
            rule_id=rule_id,
            bc_id="bc-x",
            advertiser_ids=("a-1",),
            authorization_revision=4,
            optimizer_version=7,
            rule_version=3,
        ),
    )
    assert snapshot.rule_version == 3
    assert snapshot.advertiser_ids == ("a-1",)
    assert snapshot.hits[0]["targets"][0]["key"] == target.key
    assert snapshot.skipped_accounts[0]["reason"] == "STALE_REPORT"
    assert snapshot.data_freshness["checked_count"] == 1
    assert snapshot.retention_until is not None
