from decimal import Decimal

from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import AccountInfo, Entity, Inputs
from app.modules.automation_rules.schemas import RuleDefinition
from tests.modules.automation_rules.test_definition import base


def ent(
    kind,
    adv,
    rid,
    *,
    parent=None,
    drama="嘉书-总裁",
    valid=True,
    status="ENABLE",
    currency="USD",
    name=None,
    uses=(),
    **attrs,
):
    return Entity(
        key=f"{kind}:{adv}:{rid}",
        kind=kind,
        advertiser_id=adv,
        bc_id="bc-a",
        remote_id=rid,
        name=name or rid,
        drama=drama if valid else None,
        naming_valid=valid,
        status=status,
        currency=currency,
        timezone="UTC",
        parent_key=parent,
        attributes={"name": name or rid, **attrs},
        material_uses=tuple(uses),
    )


def inputs(entities, today):
    return Inputs(
        accounts={"a": AccountInfo("a", "bc-a", "UTC", "USD", "A")},
        entities={e.key: e for e in entities},
        facts={"TODAY": today},
        skipped_accounts=[],
    )


def rule(**patch):
    return RuleDefinition.model_validate(base(**patch))


GROUPS = [
    ent("campaign", "a", "c1"),
    ent("campaign", "a", "c2"),
    ent("adgroup", "a", "g1", parent="campaign:a:c1"),
    ent("adgroup", "a", "g2", parent="campaign:a:c2"),
    ent("ad", "a", "x1", parent="adgroup:a:g1"),
]


def test_each_object_and_sum_over_sum_roas():
    facts = {
        "adgroup:a:g1": {"spend": Decimal("20"), "revenue_total": Decimal("10")},
        "adgroup:a:g2": {"spend": Decimal("5"), "revenue_total": Decimal("10")},
    }
    result = evaluate(
        rule(
            conditions=[
                {
                    "metric": "spend",
                    "range": "TODAY",
                    "operator": "GT",
                    "values": ["10"],
                },
                {
                    "metric": "roas_total",
                    "range": "TODAY",
                    "operator": "LT",
                    "values": ["1"],
                },
            ]
        ),
        inputs(GROUPS, facts),
    )
    assert result.checked == 2
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g1"]
    assert result.hits[0].metrics["roas_total:TODAY"] == "0.5"


def test_action_level_expands_to_descendants():
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}}
    result = evaluate(
        rule(action={"type": "PAUSE", "level": "ad"}), inputs(GROUPS, facts)
    )
    assert [t.key for t in result.hits[0].targets] == ["ad:a:x1"]


def test_drama_total_hits_all_members():
    facts = {
        "adgroup:a:g1": {"spend": Decimal("6")},
        "adgroup:a:g2": {"spend": Decimal("6")},
    }
    result = evaluate(rule(aggregation="DRAMA"), inputs(GROUPS, facts))
    assert len(result.hits) == 1 and result.hits[0].subject_label == "嘉书-总裁"
    assert {t.key for t in result.hits[0].targets} == {"adgroup:a:g1", "adgroup:a:g2"}


def test_drama_skips_invalid_naming():
    items = GROUPS + [ent("adgroup", "a", "g3", valid=False)]
    facts = {"adgroup:a:g3": {"spend": Decimal("50")}}
    result = evaluate(rule(aggregation="DRAMA"), inputs(items, facts))
    assert result.hits == () and result.skipped_count == 1


def test_zero_denominator_is_not_a_hit():
    facts = {"adgroup:a:g1": {"spend": Decimal("0"), "revenue_total": Decimal("0")}}
    result = evaluate(
        rule(
            aggregation="ALL",
            conditions=[
                {
                    "metric": "roas_total",
                    "range": "TODAY",
                    "operator": "LT",
                    "values": ["1"],
                }
            ],
        ),
        inputs(GROUPS, facts),
    )
    assert result.hits == ()


def test_all_objects_split_by_currency():
    items = [ent("adgroup", "a", "g1"), ent("adgroup", "a", "g2", currency="EUR")]
    facts = {
        "adgroup:a:g1": {"spend": Decimal("8")},
        "adgroup:a:g2": {"spend": Decimal("8")},
    }
    result = evaluate(rule(aggregation="ALL"), inputs(items, facts))
    assert result.hits == ()  # 各自 8,均未超过 10;不会合并成 16
    assert result.checked == 2


def test_status_filters_and_missing_facts_are_zero():
    items = [ent("adgroup", "a", "g1", status="DISABLE"), ent("adgroup", "a", "g2")]
    result = evaluate(
        rule(
            conditions=[
                {"metric": "spend", "range": "TODAY", "operator": "LT", "values": ["1"]}
            ]
        ),
        inputs(items, {}),
    )
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g2"]


def test_ignore_status_keeps_condition_check_for_paused_objects():
    # 反向冲突判断：暂停规则已把 g1 关掉，仍要看它的条件按当前数据是否成立。
    items = [
        ent("adgroup", "a", "g1", status="DISABLE"),
        ent("adgroup", "a", "g2", status="DISABLE"),
    ]
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}}
    result = evaluate(rule(), inputs(items, facts), ignore_status=True)
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g1"]
    assert evaluate(rule(), inputs(items, facts)).hits == ()


def test_name_and_attribute_conditions():
    items = [
        ent("adgroup", "a", "g1", name="嘉书-测试", budget=Decimal("30")),
        ent("adgroup", "a", "g2", name="其他", budget=Decimal("30")),
    ]
    result = evaluate(
        rule(
            conditions=[
                {"metric": "name", "operator": "STARTS_WITH", "values": ["嘉书"]},
                {"metric": "budget", "operator": "BETWEEN", "values": ["20", "40"]},
            ]
        ),
        inputs(items, {}),
    )
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g1"]


def test_selected_objects_and_name_filters():
    patch = {
        "apply_to": {"level": "adgroup", "status": "SELECTED", "object_ids": ["a:g2"]}
    }
    facts = {
        "adgroup:a:g1": {"spend": Decimal("20")},
        "adgroup:a:g2": {"spend": Decimal("20")},
    }
    assert [
        h.subject_key for h in evaluate(rule(**patch), inputs(GROUPS, facts)).hits
    ] == ["adgroup:a:g2"]


def test_material_targets_from_ads():
    use = {
        "advertiser_id": "a",
        "ad_remote_id": "x1",
        "platform_material_id": "v1",
        "ad_material_id": "m1",
        "material_type": "VIDEO",
    }
    items = GROUPS + [ent("material", "a", "v1", uses=[use])]
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}}
    result = evaluate(
        rule(
            action={"type": "PAUSE", "level": "material"},
            schedule={"frequency": "1H", "window": "CONTINUOUS"},
        ),
        inputs(items, facts),
    )
    assert [t.key for t in result.hits[0].targets] == ["material:a:v1"]


def test_total_action_level_above_judged_level_walks_up():
    facts = {
        "adgroup:a:g1": {"spend": Decimal("6")},
        "adgroup:a:g2": {"spend": Decimal("6")},
    }
    result = evaluate(
        rule(aggregation="DRAMA", action={"type": "PAUSE", "level": "campaign"}),
        inputs(GROUPS, facts),
    )
    assert [t.key for t in result.hits[0].targets] == ["campaign:a:c1", "campaign:a:c2"]


def test_all_null_metric_is_missing_not_zero():
    facts = {"adgroup:a:g1": {"spend": Decimal("5"), "revenue_d7": None}}
    result = evaluate(
        rule(
            conditions=[
                {
                    "metric": "revenue_d7",
                    "range": "TODAY",
                    "operator": "LT",
                    "values": ["10"],
                }
            ]
        ),
        inputs([ent("adgroup", "a", "g1")], facts),
    )
    assert result.hits == ()


def test_stored_targets_not_cut_by_run_limit(monkeypatch):
    from app.modules.automation_rules import evaluate as module

    facts = {
        "adgroup:a:g1": {"spend": Decimal("6")},
        "adgroup:a:g2": {"spend": Decimal("6")},
    }
    result = evaluate(rule(aggregation="DRAMA"), inputs(GROUPS, facts))
    assert len(result.hits[0].targets) == 2 and result.hits[0].target_count == 2
    # 存储只受硬上限约束，超出时 target_count 仍是真实总数。
    monkeypatch.setattr(module, "MAX_STORED_TARGETS", 1)
    result = evaluate(rule(aggregation="DRAMA"), inputs(GROUPS, facts))
    assert len(result.hits[0].targets) == 1 and result.hits[0].target_count == 2


def test_click_through_rate_is_judged_in_percent():
    # 编辑器与报表都按百分数展示点击率：填 1 表示 1%，命中指标同样显示百分数。
    facts = {
        "adgroup:a:g1": {"clicks": Decimal("2"), "impressions": Decimal("400")},
        "adgroup:a:g2": {"clicks": Decimal("6"), "impressions": Decimal("400")},
    }
    result = evaluate(
        rule(
            conditions=[
                {"metric": "ctr", "range": "TODAY", "operator": "LT", "values": ["1"]}
            ]
        ),
        inputs(GROUPS, facts),
    )
    assert [hit.subject_key for hit in result.hits] == ["adgroup:a:g1"]
    assert result.hits[0].metrics == {"ctr:TODAY": "0.5"}
