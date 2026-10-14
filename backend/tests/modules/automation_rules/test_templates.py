from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.templates import TEMPLATES
from tests.modules.automation_rules.conftest import headers


def test_templates_are_valid_notify_definitions():
    keys = [template.key for template in TEMPLATES]
    assert keys == [
        "drama_stop_loss",
        "adgroup_stop_loss",
        "scale_budget",
        "material_off",
        "spend_breaker",
        "zero_spend",
    ]
    for template in TEMPLATES:
        definition = RuleDefinition.model_validate(template.definition)
        # 模板只给示例阈值，默认仅通知，由用户改阈值后再选执行模式。
        assert definition.mode == "NOTIFY", template.key
    material = RuleDefinition.model_validate(TEMPLATES[3].definition)
    assert material.schedule.frequency == "1H"
    budget = RuleDefinition.model_validate(TEMPLATES[2].definition)
    assert budget.action.adjustment is not None
    assert budget.action.frequency is not None


def test_templates_endpoint(client, sync_seed):
    response = client.get(
        f"/api/tenants/{sync_seed.tenant_id}/automation-rules/templates",
        headers=headers(sync_seed),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["key"] for item in items][:2] == [
        "drama_stop_loss",
        "adgroup_stop_loss",
    ]
    assert items[0]["name"] == "剧级止损"
