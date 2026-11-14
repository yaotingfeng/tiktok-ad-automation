import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.modules.ai_workbench.evidence import RuleEvidenceSnapshot
from app.modules.ai_workbench.rule_history import rule_snapshot_report
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from tests.modules.automation_rules.test_evaluate import GROUPS, ent, inputs, rule


def snapshot(definition=None, source=None):
    definition = definition or rule()
    source = source or inputs(
        GROUPS,
        {
            "adgroup:a:g1": {"spend": Decimal("20")},
            "adgroup:a:g2": {"spend": Decimal("5")},
        },
    )
    owner = AutomationRule(
        tenant_id=uuid4(),
        created_by=uuid4(),
        updated_by=uuid4(),
        name="history",
        definition=definition.model_dump(mode="json"),
    )
    run = AutomationRuleRun(
        tenant_id=owner.tenant_id,
        rule_id=owner.id,
        rule_version=1,
        definition=owner.definition,
        slot_at=datetime(2026, 11, 13, tzinfo=UTC),
    )
    return RuleEvidenceSnapshot.from_evaluation(
        rule=owner,
        run=run,
        result=evaluate(definition, source),
        skipped_accounts=source.skipped_accounts,
        inputs=source,
    )


def test_freezes_nonhits_raw_facts_accounts_and_full_hierarchy():
    source = inputs(GROUPS, {"adgroup:a:g1": {"spend": Decimal("20")}})
    frozen = snapshot(source=source)
    source.facts["TODAY"]["adgroup:a:g1"]["spend"] = Decimal("999")
    assert len(frozen.checked_objects) == len(GROUPS)
    assert frozen.checked_metrics["TODAY"]["adgroup:a:g1"]["spend"] == "20"
    assert frozen.data_freshness["threshold_conditions"][0]["values"] == ["10"]
    report = rule_snapshot_report(frozen)
    assert report["replay"]["available"] is True
    assert report["replay"]["current"]["hit_subjects"] == ["adgroup:a:g1"]
    assert report["replay"]["current"]["nonhit_count"] == 1
    assert report["accounts"]["a"]["timezone"] == "UTC"


def test_threshold_replay_preserves_children_and_does_not_claim_savings():
    definition = rule(action={"type": "PAUSE", "level": "ad"})
    candidate = definition.model_copy(
        update={
            "conditions": rule(
                conditions=[
                    {
                        "metric": "spend",
                        "range": "TODAY",
                        "operator": "GT",
                        "values": ["3"],
                    }
                ]
            ).conditions
        }
    )
    report = rule_snapshot_report(snapshot(definition), candidate_definition=candidate)
    assert report["replay"]["current"]["hit_targets"] == ["ad:a:x1"]
    assert report["replay"]["comparison"]["added_hit_subjects"] == ["adgroup:a:g2"]
    assert report["causal_outcomes_available"] is False


def test_aggregate_replay_uses_all_members_and_sum_over_sum():
    definition = rule(
        aggregation="DRAMA", action={"type": "PAUSE", "level": "campaign"}
    )
    report = rule_snapshot_report(
        snapshot(
            definition,
            inputs(
                GROUPS,
                {
                    "adgroup:a:g1": {"spend": Decimal("6")},
                    "adgroup:a:g2": {"spend": Decimal("6")},
                },
            ),
        )
    )
    assert report["replay"]["current"]["checked"] == 1
    assert report["replay"]["current"]["hit_targets"] == [
        "campaign:a:c1",
        "campaign:a:c2",
    ]


def test_numeric_attributes_roundtrip_as_decimal():
    definition = rule(
        conditions=[{"metric": "budget", "operator": "GT", "values": ["10"]}]
    )
    report = rule_snapshot_report(
        snapshot(
            definition,
            inputs(
                [
                    ent("adgroup", "a", "g", budget=Decimal("20.01")),
                ],
                {},
            ),
        )
    )
    assert report["replay"]["current"]["hit_subjects"] == ["adgroup:a:g"]


def test_material_issues_and_uses_are_retained():
    definition = rule(
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
        schedule={"frequency": "1H", "window": "CONTINUOUS"},
    )
    material = replace(
        ent(
            "material",
            "a",
            "m",
            uses=[{"advertiser_id": "a", "ad_remote_id": "x"}],
            video="vid",
        ),
        judgement_issue="MATERIAL_SOURCE_CHANGED",
        report_ids=("old", "new"),
    )
    report = rule_snapshot_report(snapshot(definition, inputs([material], {})))
    assert report["objects"][0]["report_ids"] == ["old", "new"]
    assert report["objects"][0]["material_uses"][0]["ad_remote_id"] == "x"
    assert report["replay"]["current"]["skipped_count"] == 1
    assert report["complete_backtest"] is False


def test_legacy_threshold_only_snapshot_is_not_replayable():
    frozen = snapshot()
    legacy = replace(frozen, data_freshness={}, checked_metrics={"spend:TODAY": ["10"]})
    report = rule_snapshot_report(legacy)
    assert report["replay"]["available"] is False
    assert "historical_inputs_unavailable" in report["limitations"]


def test_missing_range_and_changed_account_scope_are_not_fabricated():
    frozen = snapshot()
    for candidate in [
        rule(
            conditions=[
                {
                    "metric": "spend",
                    "range": "YESTERDAY",
                    "operator": "GT",
                    "values": ["1"],
                }
            ]
        ),
        rule(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["different"]}),
    ]:
        report = rule_snapshot_report(frozen, candidate_definition=candidate)
        assert report["replay"]["candidate"]["available"] is False
        assert report["complete_backtest"] is False


def test_null_metrics_are_not_nonhits_or_complete_backtests():
    report = rule_snapshot_report(
        snapshot(
            source=inputs(
                GROUPS,
                {
                    "adgroup:a:g1": {"spend": None},
                    "adgroup:a:g2": {"spend": Decimal("5")},
                },
            )
        )
    )
    assert report["replay"]["current"]["nonhit_count"] is None
    assert report["complete_backtest"] is False
    assert "missing_metric_values" in report["limitations"]


def test_bounded_report_and_tampered_snapshot_never_claim_completeness():
    frozen = snapshot()
    report = rule_snapshot_report(frozen, object_limit=1)
    assert len(report["objects"]) == 1
    assert report["truncated"] is True
    assert report["complete_backtest"] is False
    broken = replace(frozen, checked_objects=frozen.checked_objects[:-1])
    report = rule_snapshot_report(broken)
    assert report["replay"]["available"] is False
    assert "snapshot_incomplete" in report["limitations"]


def test_complete_report_can_be_verified_and_replayed_without_current_database():
    frozen = snapshot()
    report = rule_snapshot_report(frozen)
    restored = {
        "definition": frozen.definition,
        "checked_objects": report["objects"],
        "checked_metrics": report["facts"],
        "skipped_accounts": list(frozen.skipped_accounts),
        "data_freshness": {
            "evaluation_inputs": {
                **report["input_metadata"],
                "accounts": report["accounts"],
            }
        },
    }
    assert rule_snapshot_report(restored) == report
    restored["checked_metrics"]["TODAY"]["adgroup:a:g1"]["spend"] = "10000"
    assert rule_snapshot_report(restored)["replay"]["available"] is False


def test_single_large_material_object_cannot_bypass_report_size_bound():
    report = rule_snapshot_report(
        snapshot(
            source=inputs(
                [
                    ent("adgroup", "a", "g", name="大" * 5000),
                ],
                {"adgroup:a:g": {"spend": Decimal("20")}},
            )
        ),
        report_byte_limit=1000,
    )
    assert len(json.dumps(report, ensure_ascii=False).encode()) <= 1000
    assert report["complete_backtest"] is False
    assert report["limitations"] == ["report_size_limit"]
