from decimal import Decimal

from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import AccountInfo, Inputs
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.templates import TEMPLATES
from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_evaluate import ent

KEYS = [
    "cold_start_no_delivery",
    "test_adgroup_stop_loss",
    "test_material_cull",
    "drama_test_fail",
    "scale_adgroup_budget",
    "scale_campaign_budget",
    "relax_roas_target",
    "decline_budget_trim",
    "intraday_spend_anomaly",
    "daily_spend_cap",
]


def _template(key: str) -> RuleDefinition:
    template = next(item for item in TEMPLATES if item.key == key)
    return RuleDefinition.model_validate(template.definition)


def test_templates_are_valid_notify_definitions():
    assert [template.key for template in TEMPLATES] == KEYS
    for template in TEMPLATES:
        definition = RuleDefinition.model_validate(template.definition)
        # 模板只给示例阈值，默认仅通知，由用户改阈值后再选执行模式。
        assert definition.mode == "NOTIFY", template.key
        # 出价与出价保障都按第 0 天核算，累计 ROAS 成熟度不一致，模板不用它做判断。
        assert all(c.metric != "roas_total" for c in definition.conditions)


def test_stop_and_trim_templates_judge_finished_days_once_a_day():
    # 关停、降预算、降目标不看盘中数据：当天收入仍在回传，且当天操作会影响出价保障。
    for key in (
        "test_adgroup_stop_loss",
        "test_material_cull",
        "drama_test_fail",
        "relax_roas_target",
        "decline_budget_trim",
    ):
        definition = _template(key)
        assert definition.schedule.frequency == "DAILY", key
        roas = [c for c in definition.conditions if c.metric == "roas_d0"]
        assert roas and all(c.range != "TODAY" for c in roas), key
        assert any(c.range == "YESTERDAY" for c in roas), key


def test_single_object_schedules_follow_account_timezones():
    # 单对象规则按各账户当地时间判断；跨账户合计需要同一时刻取数，按规则时区。
    for template in TEMPLATES:
        definition = RuleDefinition.model_validate(template.definition)
        daily_or_daytime = definition.schedule.frequency == "DAILY" or (
            definition.schedule.window == "CUSTOM"
        )
        if definition.aggregation != "EACH":
            assert definition.schedule.timezone_basis == "RULE", template.key
        elif daily_or_daytime:
            assert definition.schedule.timezone_basis == "ACCOUNT", template.key


def test_budget_templates_only_touch_the_budget_owner():
    # 「自身预算 > 0」把组预算模板限定在非 CBO 广告组、CBO 模板限定在持有预算的系列。
    for key in (
        "scale_adgroup_budget",
        "scale_campaign_budget",
        "decline_budget_trim",
    ):
        definition = _template(key)
        assert definition.action.level == definition.apply_to.level, key
        assert any(
            c.metric == "budget" and c.operator == "GT" and c.values == ("0",)
            for c in definition.conditions
        ), key
        assert definition.action.adjustment is not None
        assert definition.action.adjustment.limit is not None
        assert definition.action.frequency is not None


def test_adgroup_scaling_skips_groups_under_cbo_campaigns():
    winning = {
        "spend": Decimal("18"),
        "revenue_d0": Decimal("27"),
    }
    today = {"spend": Decimal("8"), "revenue_d0": Decimal("10")}
    entities = [
        ent("campaign", "a", "c1"),
        ent("adgroup", "a", "own", parent="campaign:a:c1", budget=Decimal("20")),
        # CBO 系列下的广告组没有自身预算，即使数据达标也不能命中。
        ent("adgroup", "a", "cbo", parent="campaign:a:c1", budget=None),
    ]
    facts = {
        "YESTERDAY": {"adgroup:a:own": winning, "adgroup:a:cbo": winning},
        "TODAY": {"adgroup:a:own": today, "adgroup:a:cbo": today},
    }
    result = evaluate(
        _template("scale_adgroup_budget"),
        Inputs(
            accounts={"a": AccountInfo("a", "bc-a", "UTC", "USD", "A")},
            entities={e.key: e for e in entities},
            facts=facts,
        ),
    )
    assert [hit.subject_key for hit in result.hits] == ["adgroup:a:own"]


def test_templates_endpoint(client, sync_seed):
    response = client.get(
        f"/api/tenants/{sync_seed.tenant_id}/automation-rules/templates",
        headers=headers(sync_seed),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["key"] for item in items] == KEYS
    assert items[0]["name"] == "冷启动 · 新建不起量预警"
