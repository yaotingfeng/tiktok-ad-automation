from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench import service
from app.modules.ai_workbench.proposals import (
    ProposalInput,
    _within_authorization,
    create_proposal,
)
from app.modules.ai_workbench.reviews import create_review_task
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.automation_rules.models import AutomationRule
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.ai_workbench.test_publication_modes import optimizer_version
from tests.modules.automation_rules.test_definition import base


def test_model_cannot_rebase_rule_after_human_edit(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    definition = base(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]})
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="rule",
        definition=definition,
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(rule)
    session.flush()
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(strategy.id, rule_ids=[str(rule.id)])
        ),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    rule.version += 1
    rule.name = "human update"
    session.flush()
    with pytest.raises(DomainError, match="基线|stale"):
        create_proposal(
            session,
            context=context,
            optimizer_id=optimizer.id,
            review_task=task,
            change=ProposalInput(
                kind="rule", target_id=rule.id, after={"definition": definition}
            ),
        )


def test_auto_supported_fields_are_not_gated_by_archived_authorization():
    version = optimizer_version(
        mode="AUTO",
        limits={
            "strategy": True,
            "max_budget_delta": 5,
            "rule": {"allowed": True, "allowed_fields": []},
        },
    )
    from app.modules.strategies.schemas import StrategyConfig

    baseline = StrategyConfig.model_validate(
        {
            "budget": "10",
            "currency": "USD",
            "copy_pool_version": str(uuid4()),
            "group_count": 8,
            "bid_strategy": "TARGET_ROAS",
            "target_roas": "1.5",
        }
    ).model_dump(mode="json")
    omitted = {k: baseline[k] for k in ("budget", "currency", "copy_pool_version")}
    assert _within_authorization(
        version=version,
        kind="strategy",
        before={"config": baseline},
        after={"config": omitted},
    )[0]
    definition = base(accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]})
    assert _within_authorization(
        version=version,
        kind="rule",
        before={"definition": definition, "enabled": False, "name": "old"},
        after={"definition": definition, "enabled": True},
    )[0]
    assert _within_authorization(
        version=version,
        kind="rule",
        before={"definition": definition, "enabled": False, "name": "old"},
        after={"definition": definition, "name": "new"},
    )[0]


def test_public_knowledge_import_retry_does_not_duplicate_asset(session, context):
    from app.models import User
    from app.modules.ai_workbench.knowledge import KnowledgeInput, create_version

    session.get(User, context.actor_id).is_superuser = True
    session.flush()
    body = KnowledgeInput(
        key="retry-pack",
        scope="PUBLIC",
        title="policy",
        body="same text",
        source="reviewed",
    )
    first = create_version(session, context=context, body=body)
    second = create_version(session, context=context, body=body)
    assert first.id == second.id
    with pytest.raises(DomainError, match="已有|existing"):
        create_version(
            session, context=context, body=body.model_copy(update={"body": "different"})
        )


def test_pending_confirmation_expiry_isolated_by_optimizer(session, context):
    from datetime import UTC, datetime, timedelta

    from app.modules.automation_rules.confirmations import expire_open
    from app.modules.automation_rules.models import (
        AutomationConfirmation,
        AutomationRuleRun,
    )

    strategy = seed_optimizer_dependencies(session, context)
    first = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    # 第二个优化师使用独立策略，不能通过共享策略绕过现有保护。
    second_strategy = seed_optimizer_dependencies(session, context, account="acc-2")
    second = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(second_strategy.id, advertiser_ids=["acc-2"])
        ),
    )
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name="shared-confirm",
        definition=base(mode="CONFIRM"),
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(rule)
    session.flush()
    now = datetime.now(UTC)
    batches = []
    for position, optimizer_id in enumerate((first.id, second.id, None)):
        run = AutomationRuleRun(
            tenant_id=context.tenant_id,
            rule_id=rule.id,
            optimizer_id=optimizer_id,
            rule_version=1,
            slot_at=now - timedelta(minutes=position),
            definition=rule.definition,
        )
        session.add(run)
        session.flush()
        batch = AutomationConfirmation(
            tenant_id=context.tenant_id, rule_id=rule.id, run_id=run.id
        )
        session.add(batch)
        batches.append(batch)
    session.flush()
    assert expire_open(session, rule_id=rule.id, now=now, optimizer_id=first.id) == 1
    assert [b.status for b in batches] == ["EXPIRED", "PENDING", "PENDING"]
    assert expire_open(session, rule_id=rule.id, now=now) == 1
    assert batches[1].status == "PENDING"
