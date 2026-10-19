"""将广告管理的真实回执汇总回规则运行；受理不等同于平台状态已回读验证。"""

from collections import Counter
from uuid import UUID

from sqlmodel import Session, col, select

from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTaskItem,
)
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRule,
    AutomationRuleRun,
)
from app.modules.automation_rules.runner import notify_execution


def refresh_execution_results(session: Session, task_id: UUID, tenant_id: UUID) -> None:
    run_ids = session.exec(
        select(AutomationActionLedger.run_id)
        .where(
            AutomationActionLedger.tenant_id == tenant_id,
            AutomationActionLedger.management_task_id == task_id,
        )
        .distinct()
    ).all()
    for run_id in sorted(run_ids, key=str):
        run = session.exec(
            select(AutomationRuleRun)
            .where(
                AutomationRuleRun.tenant_id == tenant_id,
                AutomationRuleRun.id == run_id,
            )
            .with_for_update()
        ).first()
        if run is None:
            continue
        task_ids = session.exec(
            select(AutomationActionLedger.management_task_id)
            .where(
                AutomationActionLedger.tenant_id == tenant_id,
                AutomationActionLedger.run_id == run_id,
            )
            .distinct()
        ).all()
        items = session.exec(
            select(ManagementTaskItem.id, ManagementTaskItem.execution_result).where(
                ManagementTaskItem.tenant_id == tenant_id,
                col(ManagementTaskItem.task_id).in_(task_ids),
            )
        ).all()
        results: Counter[str] = Counter()
        for item_id, result in items:
            if result == "NOT_SENT":
                attempt = session.exec(
                    select(ManagementRequestAttempt.retryable)
                    .where(
                        ManagementRequestAttempt.tenant_id == tenant_id,
                        ManagementRequestAttempt.task_item_id == item_id,
                    )
                    .order_by(col(ManagementRequestAttempt.attempt).desc())
                    .limit(1)
                ).first()
                if attempt:
                    result = "PENDING"
            results[result.lower()] += 1
        summary = {**run.execution_summary, "results": dict(results)}
        previous_status = run.execution_status
        if results["pending"]:
            status = "SUBMITTED"
        elif results["unknown"]:
            status = "NEEDS_REVIEW"
        elif (
            results["accepted"] + results["no_change"] == len(items)
            and not summary.get("failed")
            and not summary.get("conflicts")
        ):
            status = "ACCEPTED"
        elif results["accepted"] or results["no_change"]:
            status = "PARTIAL"
        else:
            status = "FAILED"
        if run.execution_summary == summary and previous_status == status:
            continue
        run.execution_summary, run.execution_status = summary, status
        # 所有管理项结算后再发结果卡片；重投只更新一次，等待重试不反复通知。
        if not results["pending"]:
            rule = session.get(AutomationRule, run.rule_id)
            if rule is not None and rule.tenant_id == tenant_id:
                notify_execution(session, run=run, rule=rule, phase="result")
        session.add(run)
    session.flush()
