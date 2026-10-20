"""真实 PostgreSQL 验证规则和运行快照一起迁移，且其他定义不变。"""

from datetime import UTC, datetime

from alembic import command
from sqlmodel import Session, select

from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from app.modules.automation_rules.schemas import RuleDefinition
from tests.migration_database import historical_database
from tests.modules.automation_rules.test_definition import base
from tests.modules.conftest import create_context


def test_frequency_upgrade_and_downgrade_cover_rules_and_run_snapshots(monkeypatch):
    with historical_database(monkeypatch, "0040_material_production") as (
        engine,
        config,
    ):
        originals = {}
        with Session(engine) as session, session.begin():
            context = create_context(session, role="tenant_admin")
            for frequency in ("5M", "30M"):
                definition = base(
                    schedule={"frequency": frequency, "window": "CONTINUOUS"}
                )
                rule = AutomationRule(
                    tenant_id=context.tenant_id,
                    name=frequency,
                    created_by=context.actor_id,
                    updated_by=context.actor_id,
                    definition=definition,
                )
                session.add(rule)
                session.flush()
                run = AutomationRuleRun(
                    tenant_id=context.tenant_id,
                    rule_id=rule.id,
                    rule_version=1,
                    definition=definition,
                    slot_at=datetime.now(UTC),
                )
                session.add(run)
                session.flush()
                originals[rule.id] = originals[run.id] = definition
        command.upgrade(config, "0041_rule_frequency")
        with Session(engine) as session:
            for model in (AutomationRule, AutomationRuleRun):
                for row in session.exec(select(model)).all():
                    expected = originals[row.id] | {
                        "schedule": originals[row.id]["schedule"]
                        | {
                            "frequency": "15M"
                            if originals[row.id]["schedule"]["frequency"] == "5M"
                            else "30M"
                        }
                    }
                    assert row.definition == expected
                    assert (
                        RuleDefinition.model_validate(row.definition).schedule.frequency
                        == expected["schedule"]["frequency"]
                    )
        command.downgrade(config, "0040_material_production")
        with Session(engine) as session:
            for model in (AutomationRule, AutomationRuleRun):
                for row in session.exec(select(model)).all():
                    assert row.definition == originals[row.id]
