from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from tests.modules.conftest import create_context


def rule(context, name="规则"):
    return AutomationRule(
        tenant_id=context.tenant_id,
        name=name,
        definition={"x": 1},
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )


def test_rule_name_unique_per_tenant(session):
    a, b = create_context(session), create_context(session)
    session.add_all([rule(a), rule(b)])
    session.flush()
    session.add(rule(a))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_run_per_slot(session):
    context = create_context(session)
    item = rule(context)
    session.add(item)
    session.flush()
    slot = datetime(2026, 10, 14, 1, 0, tzinfo=UTC)
    for _ in range(2):
        session.add(
            AutomationRuleRun(
                tenant_id=context.tenant_id,
                rule_id=item.id,
                rule_version=1,
                definition={},
                slot_at=slot,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()
