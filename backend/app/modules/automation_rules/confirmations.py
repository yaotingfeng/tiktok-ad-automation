"""待确认批次：确认后执行（或自动执行超出单次上限）的命中先放在这里，由人确认后执行。"""

from datetime import datetime

from sqlmodel import Session, col, select

from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)


def open_batch(
    session: Session, *, run: AutomationRuleRun, rule: AutomationRule, now: datetime
) -> AutomationConfirmation:
    batch = AutomationConfirmation(
        tenant_id=rule.tenant_id, rule_id=rule.id, run_id=run.id, created_at=now
    )
    session.add(batch)
    for hit in session.exec(
        select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)
    ).all():
        hit.outcome = "PENDING_CONFIRMATION"
    run.execution_status = "AWAITING_CONFIRMATION"
    session.flush()
    return batch


def expire_open(session: Session, *, rule_id: object, now: datetime) -> int:
    """规则开始新一轮运行时，上一轮没处理的批次失效，避免按过期的结论执行。"""
    batches = session.exec(
        select(AutomationConfirmation)
        .where(
            AutomationConfirmation.rule_id == rule_id,
            AutomationConfirmation.status == "PENDING",
        )
        .with_for_update()
    ).all()
    for batch in batches:
        batch.status, batch.decided_at = "EXPIRED", now
        run = session.get(AutomationRuleRun, batch.run_id)
        if run is not None:
            run.execution_status = "EXPIRED"
            for hit in session.exec(
                select(AutomationRuleHit).where(
                    AutomationRuleHit.run_id == run.id,
                    col(AutomationRuleHit.outcome) == "PENDING_CONFIRMATION",
                )
            ).all():
                hit.outcome = "NOT_SELECTED"
    session.flush()
    return len(batches)
