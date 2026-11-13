"""任务范围历史规则回测：仅重放已冻结事实，不读当前报表或执行广告动作。"""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ai_workbench.input_budget import MAX_INPUT_BYTES
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.rule_history import rule_snapshot_report
from app.modules.automation_rules.models import AutomationRuleEvidence


def backtest_for_task(
    session, *, task, evidence_id: UUID, candidate_definition: dict
) -> dict:
    from app.modules.automation_rules.schemas import RuleDefinition

    candidate = RuleDefinition.model_validate(candidate_definition).model_dump(
        mode="json"
    )
    frozen = next(
        (
            row
            for row in task.evidence.get("rule_evidence", [])
            if row["id"] == str(evidence_id)
        ),
        None,
    )
    if frozen is None:
        raise HTTPException(404, "Frozen rule evidence not found")
    digest = hashlib.sha256(
        json.dumps(
            [str(evidence_id), candidate], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    previous = task.evidence.get("rule_backtests", [])
    cached = next((row for row in previous if row["request_digest"] == digest), None)
    if cached:
        return cached
    if len(previous) >= 10:
        raise DomainError("ai_backtest_budget", "每轮最多十次历史候选规则回测")
    source = session.exec(
        select(AutomationRuleEvidence).where(
            AutomationRuleEvidence.id == evidence_id,
            AutomationRuleEvidence.tenant_id == task.tenant_id,
            AutomationRuleEvidence.optimizer_id == task.optimizer_id,
            AutomationRuleEvidence.bc_id == task.evidence["optimizer"]["bc_id"],
        )
    ).first()
    if source is None or not set(source.advertiser_ids).issubset(
        set(task.evidence["optimizer"]["advertiser_ids"])
    ):
        raise HTTPException(404, "Frozen rule evidence not found")
    if source.definition != frozen["definition"] or source.data_freshness.get(
        "evaluation_inputs", {}
    ).get("digest") != frozen.get("historical_inputs_digest"):
        raise DomainError("ai_backtest_source_changed", "历史事实与冻结复盘不一致")
    result = sanitize_public(
        {
            "id": str(uuid5(NAMESPACE_URL, f"tkada:{task.id}:{digest}")),
            "request_digest": digest,
            "evidence_id": str(evidence_id),
            "candidate_definition": candidate,
            "result": rule_snapshot_report(source, candidate_definition=candidate),
            "interpretation": "Historical hit differences only. Does not establish actual avoided losses, false pauses, causal lift or future results.",
        }
    )
    evidence = {**task.evidence, "rule_backtests": [*previous, result]}
    if (
        len(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_INPUT_BYTES
    ):
        raise DomainError("ai_review_input_too_large", "历史回测归档超过复盘容量")
    task.evidence = evidence
    session.flush()
    return result
