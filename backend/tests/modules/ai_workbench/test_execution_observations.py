"""真实 PostgreSQL 证据连接：受理≠观察、时间截断及账户/优化师/租户隔离。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.ad_management.models import (
    ManagementReceipt,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ai_workbench import service
from app.modules.ai_workbench.execution_observations import freeze_rule_execution
from app.modules.ai_workbench.models import AIOptimizer
from app.modules.ai_workbench.reviews import load_optimizer
from app.modules.ai_workbench.schemas import OptimizerCreate
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRuleEvidence,
)
from tests.modules.ai_workbench.test_optimizer import (
    create_body,
    seed_optimizer_dependencies,
)


@pytest.fixture
def facts(session, context):
    strategy = seed_optimizer_dependencies(session, context)
    optimizer = service.create_optimizer(
        session,
        context=context,
        body=OptimizerCreate.model_validate(create_body(strategy.id)),
    )
    _, version = load_optimizer(session, context, optimizer.id)
    now = datetime.now(UTC)
    evidence = AutomationRuleEvidence(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        bc_id=version.bc_id,
        advertiser_ids=version.advertiser_ids,
        rule_version=1,
        definition={},
        created_at=now - timedelta(minutes=5),
        retention_until=now + timedelta(days=30),
    )
    task = ManagementTask(
        tenant_id=context.tenant_id,
        bc_id=version.bc_id,
        actor_id=context.actor_id,
        source_kind="DIRECT",
        request={"mutation": {}, "selection": {}},
        idempotency_key=uuid4(),
        digest="a" * 64,
        created_at=now - timedelta(minutes=4),
        updated_at=now - timedelta(minutes=2),
    )
    session.add_all([evidence, task])
    session.flush()
    item = ManagementTaskItem(
        tenant_id=context.tenant_id,
        task_id=task.id,
        source_kind="DIRECT",
        ref={
            "tenant_id": str(context.tenant_id),
            "advertiser_id": "acc-1",
            "kind": "campaign",
            "remote_id": "campaign-1",
        },
        original_value="ENABLE",
        final_value="DISABLE",
        capability={"operation": "set_status"},
        execution_result="ACCEPTED",
        observation_state="ACCEPTED",
    )
    ledger = AutomationActionLedger(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        evidence_id=evidence.id,
        target_key="campaign:acc-1:campaign-1",
        action="PAUSE",
        management_task_id=task.id,
        created_at=now - timedelta(minutes=3),
    )
    session.add_all([item, ledger])
    session.flush()
    evidence.action_ledger_refs = [str(ledger.id)]
    attempt = ManagementRequestAttempt(
        tenant_id=context.tenant_id,
        task_item_id=item.id,
        outcome="ACCEPTED",
        request_at=now - timedelta(minutes=2),
    )
    session.add(attempt)
    session.flush()
    session.add(
        ManagementReceipt(
            tenant_id=context.tenant_id,
            attempt_id=attempt.id,
            outcome="ACCEPTED",
            observed_at=now - timedelta(minutes=1),
            response={"remote_code": 0},
        )
    )
    session.flush()
    return SimpleNamespace(
        session=session,
        context=context,
        optimizer=optimizer,
        version=version,
        now=now,
        evidence=evidence,
        task=task,
        item=item,
        ledger=ledger,
        attempt=attempt,
    )


def freeze(facts, **kwargs):
    return freeze_rule_execution(
        facts.session,
        context=facts.context,
        optimizer_id=facts.optimizer.id,
        version=facts.version,
        evidence=facts.evidence,
        cutoff=kwargs.get("cutoff", facts.now),
    )


def observe(facts, *, matches=True, state=None, when=None):
    when = when or facts.now - timedelta(seconds=10)
    state = state or ("OBSERVED_MATCH" if matches else "OBSERVED_DIFFERENT")
    receipt = ManagementReceipt(
        tenant_id=facts.context.tenant_id,
        attempt_id=facts.attempt.id,
        outcome="UNKNOWN",
        observed_at=when,
        response={
            "kind": "state_observation",
            "state": state,
            "value": "DISABLE" if matches else "ENABLE",
            "matches": matches,
            "remote_observed_at": (when - timedelta(seconds=1)).isoformat(),
        },
    )
    facts.item.observation_state = state
    facts.session.add(receipt)
    facts.session.flush()
    return receipt


def test_accepted_without_actual_readback_remains_required(facts):
    result = freeze(facts)
    assert result["status"] == "READBACK_REQUIRED"
    item = result["items"][0]
    assert item["execution_result"] == item["observation_state"] == "ACCEPTED"
    assert item["observation"] is None
    # 即使可变项声称已观察，也必须存在带值和时间的正式回执。
    facts.item.observation_state = "OBSERVED_MATCH"
    assert freeze(facts)["status"] == "READBACK_REQUIRED"


@pytest.mark.parametrize(
    "matches,status", [(True, "VERIFIED"), (False, "OBSERVED_DIFFERENT")]
)
def test_real_observation_preserves_actual_value_time_and_original_outcome(
    facts, matches, status
):
    receipt = observe(facts, matches=matches)
    result = freeze(facts)
    assert result["status"] == status
    item = result["items"][0]
    assert item["execution_result"] == "ACCEPTED"
    assert item["desired_value"] == "DISABLE"
    assert item["observation"]["actual_value"] == ("DISABLE" if matches else "ENABLE")
    assert item["observation"]["receipt_id"] == str(receipt.id)
    assert (
        item["observation"]["remote_observed_at"]
        == receipt.response["remote_observed_at"]
    )


def test_future_receipt_is_excluded_and_frozen_result_is_independent(facts):
    before = freeze(facts)
    observe(facts, when=facts.now + timedelta(minutes=1))
    result = freeze(facts)
    assert result["status"] == "READBACK_REQUIRED"
    assert result["items"][0]["observation"] is None
    assert result["items"][0]["observation_state"] is None
    later = freeze(facts, cutoff=facts.now + timedelta(minutes=2))
    assert later["status"] == "VERIFIED"
    assert before["status"] == "READBACK_REQUIRED"
    assert before["items"][0]["observation_state"] == "ACCEPTED"


@pytest.mark.parametrize("bad", ["missing_value", "future_remote_time", "failed"])
def test_incomplete_or_failed_observation_never_claims_verified(facts, bad):
    receipt = observe(facts)
    response = dict(receipt.response)
    if bad == "missing_value":
        response.pop("value")
    elif bad == "future_remote_time":
        response["remote_observed_at"] = (facts.now + timedelta(days=1)).isoformat()
    else:
        response = {
            "kind": "state_observation",
            "state": "RECONCILE_FAILED",
            "reason": "read_evidence_unavailable",
        }
    receipt.response = response
    facts.session.flush()
    result = freeze(facts)
    assert result["status"] == "UNKNOWN"
    assert result["items"][0]["observation"]["valid"] is False


def test_other_account_and_bc_task_items_are_not_exposed(facts):
    facts.item.ref = {**facts.item.ref, "advertiser_id": "other-account"}
    facts.session.flush()
    assert freeze(facts)["items"] == []
    assert freeze(facts)["status"] == "UNKNOWN"
    facts.item.ref = {**facts.item.ref, "advertiser_id": "acc-1"}
    facts.task.bc_id = "other-bc"
    facts.session.flush()
    assert freeze(facts)["items"] == []


def test_other_optimizer_or_tenant_cannot_supply_action_links(facts, other_context):
    other = AIOptimizer(
        tenant_id=facts.context.tenant_id,
        name="other",
        created_by=facts.context.actor_id,
    )
    facts.session.add(other)
    facts.session.flush()
    facts.ledger.optimizer_id = other.id
    facts.session.flush()
    assert freeze(facts)["status"] == "UNKNOWN"
    assert freeze(facts)["items"] == []
    result = freeze_rule_execution(
        facts.session,
        context=other_context,
        optimizer_id=facts.optimizer.id,
        version=facts.version,
        evidence=facts.evidence,
        cutoff=facts.now,
    )
    assert result["status"] == "OUT_OF_SCOPE"
    assert result["items"] == []


def test_next_round_learning_distinguishes_observed_and_pending(facts):
    from app.modules.ai_workbench.learning import freeze_history
    from app.modules.ai_workbench.proposals import AIProposal
    from app.modules.ai_workbench.reviews import DSHReviewTask
    from app.modules.automation_rules.models import AutomationRule

    rule = AutomationRule(
        tenant_id=facts.context.tenant_id,
        name="test-rule",
        definition={},
        created_by=facts.context.actor_id,
        updated_by=facts.context.actor_id,
    )
    review = DSHReviewTask(
        tenant_id=facts.context.tenant_id,
        optimizer_id=facts.optimizer.id,
        request_id=uuid4(),
        dsh_task_id="test-readback",
        fence="test",
        optimizer_version=1,
        authorization_expires_at=facts.now + timedelta(minutes=5),
        created_by=facts.context.actor_id,
        created_at=facts.now - timedelta(hours=1),
        evidence={"optimizer": {"bc_id": "bc-a", "advertiser_ids": ["acc-1"]}},
    )
    facts.session.add_all([rule, review])
    facts.session.flush()
    facts.evidence.rule_id = rule.id
    proposal = AIProposal(
        tenant_id=facts.context.tenant_id,
        optimizer_id=facts.optimizer.id,
        review_task_id=review.id,
        kind="rule",
        status="ACCEPTED",
        base_optimizer_version=1,
        base_rule_version=0,
        published_version_id=rule.id,
        request_id=uuid4(),
        created_by=facts.context.actor_id,
        decision_at=facts.now - timedelta(minutes=10),
        created_at=facts.now - timedelta(minutes=15),
    )
    facts.session.add(proposal)
    facts.session.flush()

    def history():
        return freeze_history(
            facts.session,
            context=facts.context,
            optimizer_id=facts.optimizer.id,
            version=facts.version,
            data_pack={"cutoff": facts.now.isoformat(), "reporting": []},
        )["proposals"][0]

    assert history()["execution_status"] == "READBACK_REQUIRED"
    observe(facts, matches=False)
    assert history()["execution_status"] == "OBSERVED_DIFFERENT"
    receipt = facts.session.exec(
        select(ManagementReceipt).where(
            ManagementReceipt.attempt_id == facts.attempt.id,
            ManagementReceipt.response["kind"].astext == "state_observation",
        )
    ).one()
    receipt.response = {
        **receipt.response,
        "state": "OBSERVED_MATCH",
        "value": "DISABLE",
        "matches": True,
    }
    facts.session.flush()
    assert history()["execution_status"] == "VERIFIED"
    assert history()["assessment"] == "INSUFFICIENT_EVIDENCE"


def test_rule_accepted_results_schedule_one_read_only_observation(facts, monkeypatch):
    from app.jobs.models import PendingDispatch
    from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
    from app.modules.automation_rules.results import refresh_execution_results

    rule = AutomationRule(
        tenant_id=facts.context.tenant_id,
        name="readback-rule",
        definition={},
        created_by=facts.context.actor_id,
        updated_by=facts.context.actor_id,
    )
    facts.session.add(rule)
    facts.session.flush()
    run = AutomationRuleRun(
        tenant_id=facts.context.tenant_id,
        rule_id=rule.id,
        rule_version=1,
        definition={},
        slot_at=facts.now,
        evidence_id=facts.evidence.id,
        optimizer_id=facts.optimizer.id,
    )
    facts.session.add(run)
    facts.session.flush()
    facts.ledger.run_id = run.id
    monkeypatch.setattr(
        "app.modules.automation_rules.results.notify_execution",
        lambda *args, **kwargs: None,
    )
    facts.session.flush()
    for _ in range(2):
        refresh_execution_results(facts.session, facts.task.id, facts.context.tenant_id)
    assert facts.item.execution_result == "ACCEPTED"
    assert facts.item.observation_state == "RECONCILE_REQUESTED"
    dispatches = facts.session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == facts.context.tenant_id,
            PendingDispatch.task_name == "ad_management.reconcile",
        )
    ).all()
    assert len(dispatches) == 1
    assert freeze(facts)["status"] == "READBACK_REQUIRED"


# 以下夹具在独立事务中提交任务，真实核查函数可用自己的数据库连接落回执。
from tests.modules.ad_management.test_submission_concurrency import (  # noqa: E402
    committed_management_env as committed_management_env,
)
from tests.modules.ad_management.test_unknown_reconciliation import (  # noqa: E402
    unknown_task as unknown_task,
)


@pytest.mark.parametrize(
    "value,state", [("DISABLE", "OBSERVED_MATCH"), ("ENABLE", "OBSERVED_DIFFERENT")]
)
def test_accepted_task_reads_platform_and_records_actual_observation(
    unknown_task, monkeypatch, value, state
):
    from contextlib import contextmanager

    from sqlmodel import Session

    from app.core.db import engine
    from app.integrations.tiktok.contracts.ads import AdEntity, DirectoryPage
    from app.integrations.tiktok.contracts.common import CallEvidence
    from app.modules.ad_management.reconciliation import (
        queue_reconciliation,
        reconcile_unknown_items,
    )

    context, route, task_id, item_id, ref = unknown_task
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        item.execution_result = "ACCEPTED"
        item.observation_state = "ACCEPTED"
        task = session.get(ManagementTask, task_id)
        queue_reconciliation(session, task, (item_id,))
        session.commit()
    calls = []

    def read(query):
        calls.append(query)
        return DirectoryPage(
            items=(
                AdEntity(
                    ref,
                    None,
                    "REGULAR",
                    "name",
                    {},
                    value,
                    None,
                    None,
                    datetime.now(UTC),
                ),
            ),
            materials=(),
            complete=True,
            next_page=None,
            evidence=CallEvidence(request_id="actual-read"),
        )

    @contextmanager
    def gateway(**kwargs):
        assert kwargs["route"] == route
        kwargs["before_request"]()
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", gateway
    )
    for _ in range(2):
        reconcile_unknown_items(
            engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
        )
    assert len(calls) == 1
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "ACCEPTED"
        assert item.observation_state == state
        receipt = session.exec(
            select(ManagementReceipt)
            .join(
                ManagementRequestAttempt,
                ManagementRequestAttempt.id == ManagementReceipt.attempt_id,
            )
            .where(
                ManagementRequestAttempt.task_item_id == item_id,
                ManagementReceipt.response["kind"].astext == "state_observation",
            )
        ).one()
        assert receipt.response["value"] == value
        assert receipt.response["remote_observed_at"]
        assert receipt.response["state"] == state


@pytest.mark.parametrize("pending", [True, False])
def test_late_directory_refresh_cannot_replace_real_observation(unknown_task, pending):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.ad_management.execution import _set_refresh_state

    _, _, _, item_id, _ = unknown_task
    with Session(engine) as session, session.begin():
        item = session.get(ManagementTaskItem, item_id)
        item.execution_result = "ACCEPTED"
        item.observation_state = "OBSERVED_MATCH"
    _set_refresh_state(engine, item_id, pending=pending)
    with Session(engine) as session:
        assert (
            session.get(ManagementTaskItem, item_id).observation_state
            == "OBSERVED_MATCH"
        )
