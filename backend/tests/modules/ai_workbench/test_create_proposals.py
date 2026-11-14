"""真实领域服务验证创建提案的确认、绑定和账户边界。"""

from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.ai_workbench import service
from app.modules.ai_workbench.proposals import (
    ProposalInput,
    accept_proposal,
    create_proposal,
    publish_auto_proposals,
)
from app.modules.ai_workbench.reviews import create_review_task
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.automation_rules.models import AutomationRule
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)
from tests.modules.automation_rules.test_definition import base


@pytest.mark.parametrize("kind", ["strategy", "rule"])
@pytest.mark.parametrize("mode", ["MANUAL", "AUTO"])
def test_create_proposal_uses_domain_service_and_binds_result(
    session, context, kind, mode
):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(
            create_body(strategy.id, optimization_mode=mode)
        ),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    after = (
        {"name": "AI 新配置", "config": strategy.config}
        if kind == "strategy"
        else {
            "name": "AI 新规则",
            "definition": base(
                accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]}
            ),
        }
    )
    proposal = create_proposal(
        session,
        context=context,
        optimizer_id=optimizer.id,
        review_task=task,
        change=ProposalInput(kind=kind, operation="create", after=after),
    )
    assert proposal.status == "PENDING"
    if mode == "AUTO":
        publish_auto_proposals(
            session, context=context, optimizer_id=optimizer.id, review_task_id=task.id
        )
    else:
        assert (
            publish_auto_proposals(
                session,
                context=context,
                optimizer_id=optimizer.id,
                review_task_id=task.id,
            )
            == []
        )
        accept_proposal(
            session, context=context, optimizer_id=optimizer.id, proposal_id=proposal.id
        )
    assert proposal.status == "ACCEPTED"
    current = service.get_optimizer(session, context=context, optimizer_id=optimizer.id)
    if kind == "strategy":
        assert current.strategy_version_id != strategy.id
        assert current.strategy_version_id == proposal.published_version_id
    else:
        assert proposal.published_version_id in current.rule_ids
        assert session.get(AutomationRule, proposal.published_version_id).definition[
            "accounts"
        ]["advertiser_ids"] == ["acc-1"]


def test_create_rule_cannot_expand_to_entire_bc(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    task = create_review_task(
        session, context=context, optimizer_id=optimizer.id, request_id=uuid4()
    )
    with pytest.raises(DomainError, match="管理账户"):
        create_proposal(
            session,
            context=context,
            optimizer_id=optimizer.id,
            review_task=task,
            change=ProposalInput(
                kind="rule",
                operation="create",
                after={
                    "name": "broad",
                    "definition": base(accounts={"mode": "BC", "bc_ids": ["bc-a"]}),
                },
            ),
        )
