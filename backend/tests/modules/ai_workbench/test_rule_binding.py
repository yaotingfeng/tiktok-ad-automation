import inspect
from datetime import UTC, datetime
from uuid import uuid4

from app.modules.ai_workbench.evidence import RuleEvidenceSnapshot
from app.modules.ai_workbench.rule_binding import (
    RuleBindingScope,
    _rule_accounts,
    validate_rule_binding,
)
from app.modules.automation_rules.evaluate import Evaluation, Hit, Target
from app.modules.automation_rules.models import (
    AutomationRule,
    AutomationRuleRun,
)
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
        freshness=[
            {
                "source": "report_sync_state",
                "stat_date": "2026-11-12",
                "timezone": "UTC",
                "observed_at": "2026-11-12T04:00:00+00:00",
                "publication_version": 9,
            }
        ],
        publication_versions={"a-1:ad_day:2026-11-12": 9},
    )
    assert snapshot.rule_version == 3
    assert snapshot.advertiser_ids == ("a-1",)
    assert snapshot.hits[0]["targets"][0]["key"] == target.key
    assert snapshot.skipped_accounts[0]["reason"] == "STALE_REPORT"
    assert snapshot.data_freshness["checked_count"] == 1
    assert snapshot.data_freshness["rows"][0]["observed_at"]
    assert snapshot.publication_versions["a-1:ad_day:2026-11-12"] == 9
    assert snapshot.retention_until is not None


def test_scope_validation_fences_each_connection_revision(monkeypatch):
    from app.modules.ai_workbench import rule_binding

    tenant_id, optimizer_id, rule_id = uuid4(), uuid4(), uuid4()
    scope = RuleBindingScope(
        tenant_id=tenant_id,
        optimizer_id=optimizer_id,
        rule_id=rule_id,
        bc_id="bc-x",
        advertiser_ids=("a-1",),
        authorization_revision=0,
        authorization_revisions=(("a-1:connection-a", 0), ("a-2:connection-b", 4)),
        optimizer_version=2,
        rule_version=3,
        binding_id=uuid4(),
    )
    monkeypatch.setattr(rule_binding, "resolve_rule_binding", lambda *_args, **_kwargs: scope)
    assert validate_rule_binding(object(), scope).authorization_revisions[-1][1] == 4
    changed = RuleBindingScope(
        **{
            **scope.__dict__,
            "authorization_revisions": (("a-1:connection-a", 0), ("a-2:connection-b", 5)),
        }
    )
    monkeypatch.setattr(rule_binding, "resolve_rule_binding", lambda *_args, **_kwargs: changed)
    try:
        validate_rule_binding(object(), scope)
    except Exception as error:
        assert getattr(error, "code", None) == "ai_rule_binding_changed"
    else:
        raise AssertionError("authorization revision change must fail closed")


def test_ordinary_and_optimizer_runs_have_distinct_partial_uniqueness_indexes():
    indexes = {index.name for index in AutomationRuleRun.__table__.indexes}
    assert {
        "uq_automation_run_unscoped_slot",
        "uq_automation_run_optimizer_slot",
    } <= indexes


def test_submit_boundary_revalidation_runs_after_preview_preparation():
    from app.modules.automation_rules.executor import _execute_bc

    source = inspect.getsource(_execute_bc)
    assert source.index("prepare_preview(") < source.index("validate_rule_binding(")
    assert source.index("validate_rule_binding(") < source.index("submit_management_task(")


def test_failed_submit_fence_restores_preview_unsupported_details():
    from app.modules.automation_rules.executor import _restore_bc_summary

    summary = {"submitted": 0, "unsupported_details": [{"target_key": "rolled-back"}]}
    _restore_bc_summary(
        summary,
        {"submitted": 0},
        [{"target_key": "before-bc"}],
    )
    assert summary["unsupported_details"] == [{"target_key": "before-bc"}]
