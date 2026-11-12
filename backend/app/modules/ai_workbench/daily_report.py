"""Tenant-scoped daily report assembled from frozen workbench evidence.

The report is a read model.  It joins the persisted plan, build handoff,
rule-evidence and proposal records, but never asks the reporting provider for a
second set of metrics.  Review and execution therefore continue to point at the
same immutable evidence IDs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerRuleBinding,
    BuildOccurrence,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.automation_rules.models import AutomationRule, AutomationRuleEvidence

TERMINAL_BUILD_STATUSES = {"COMPLETE", "PARTIAL", "FAILED", "UNKNOWN"}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _evidence_ids_from_value(value: Any) -> set[str]:
    """Extract only IDs already frozen by execution/review contracts."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key in ("evidence_id", "evidence_ids", "rule_evidence_id"):
            candidate = value.get(key)
            if isinstance(candidate, (str, UUID)):
                result.add(str(candidate))
            elif isinstance(candidate, (list, tuple, set)):
                result.update(str(item) for item in candidate)
        for nested in value.values():
            result.update(_evidence_ids_from_value(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            result.update(_evidence_ids_from_value(nested))
    return result


def _next_trigger(
    *,
    occurrences: list[BuildOccurrence],
    rules: list[AutomationRule],
    now: datetime,
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for row in occurrences:
        if row.status in TERMINAL_BUILD_STATUSES or row.scheduled_at < now:
            continue
        candidates.append(
            (
                row.scheduled_at,
                {"kind": "build", "id": str(row.id), "at": row.scheduled_at},
            )
        )
    for rule in rules:
        if rule.enabled and rule.next_run_at and rule.next_run_at >= now:
            candidates.append(
                (
                    rule.next_run_at,
                    {"kind": "rule", "id": str(rule.id), "at": rule.next_run_at},
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def build_daily_report(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    business_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the daily progress/readback summary for one tenant and optimizer."""
    # Reviews/proposals import evidence and reporting models.  Keep these
    # imports inside the read operation so API module discovery cannot create a
    # circular import while SQLModel registers its tables.
    from app.modules.ai_workbench import proposals, reviews

    reviews.authorize(session, context)
    optimizer = session.exec(
        select(AIOptimizer).where(
            AIOptimizer.tenant_id == context.tenant_id,
            AIOptimizer.id == optimizer_id,
        )
    ).one_or_none()
    if optimizer is None:
        from app.core.errors import DomainError

        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    version = reviews.load_optimizer(session, context, optimizer_id)[1]
    rows = session.exec(
        select(AIDailyPlanRow)
        .where(
            AIDailyPlanRow.tenant_id == context.tenant_id,
            AIDailyPlanRow.optimizer_id == optimizer_id,
            AIDailyPlanRow.business_date == business_date,
        )
        .order_by(col(AIDailyPlanRow.position))
    ).all()
    plan_ids = {row.id for row in rows}
    occurrences = (
        session.exec(
            select(BuildOccurrence)
            .where(
                BuildOccurrence.tenant_id == context.tenant_id,
                BuildOccurrence.optimizer_id == optimizer_id,
                col(BuildOccurrence.plan_row_id).in_(plan_ids),
            )
            .order_by(col(BuildOccurrence.scheduled_at), col(BuildOccurrence.id))
        ).all()
        if plan_ids
        else []
    )
    occurrence_by_plan = {row.plan_row_id: row for row in occurrences}

    evidence_rows = session.exec(
        select(AutomationRuleEvidence)
        .where(
            AutomationRuleEvidence.tenant_id == context.tenant_id,
            AutomationRuleEvidence.optimizer_id == optimizer_id,
        )
        .order_by(col(AutomationRuleEvidence.created_at).desc())
    ).all()
    # Include only decision snapshots whose frozen freshness explicitly covers
    # this business day; no metric is recomputed here.
    day_text = business_date.isoformat()
    day_evidence = [
        row
        for row in evidence_rows
        if day_text
        in {
            str(item.get("stat_date"))
            for item in (
                row.data_freshness.get("rows", [])
                if isinstance(row.data_freshness, dict)
                else []
            )
            if isinstance(item, dict) and item.get("stat_date")
        }
        or _iso(row.created_at).startswith(day_text)
    ]

    review_tasks = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            reviews.DSHReviewTask.tenant_id == context.tenant_id,
            reviews.DSHReviewTask.optimizer_id == optimizer_id,
        )
        .order_by(col(reviews.DSHReviewTask.created_at).desc())
    ).all()
    pending = proposals.list_proposals(
        session, context=context, optimizer_id=optimizer_id, status="PENDING"
    )

    frozen_ids: set[str] = {str(row.id) for row in day_evidence}
    for task in review_tasks:
        frozen_ids.update(reviews.frozen_evidence_ids(task.evidence))
    for occurrence in occurrences:
        frozen_ids.update(_evidence_ids_from_value(occurrence.evidence))

    plan_public: list[dict[str, Any]] = []
    target_counts = {
        "planned": len(rows),
        "completed": 0,
        "partial": 0,
        "pending": 0,
        "failed": 0,
        "unknown": 0,
    }
    material = {
        "requested": 0,
        "not_requested": 0,
        "pending": 0,
        "matched": 0,
        "unknown": 0,
    }
    build_outcomes: list[dict[str, Any]] = []
    for row in rows:
        occurrence = occurrence_by_plan.get(row.id)
        status = occurrence.status if occurrence else "NOT_DISPATCHED"
        if status == "COMPLETE":
            target_counts["completed"] += 1
        elif status == "PARTIAL":
            # PARTIAL 已有平台回执，是可观察的终态；不能继续显示为待执行。
            target_counts["partial"] += 1
        elif status == "UNKNOWN":
            target_counts["unknown"] += 1
        elif status == "FAILED":
            target_counts["failed"] += 1
        else:
            target_counts["pending"] += 1
        if row.order_materials:
            material["requested"] += 1
            if status == "WAITING_MATERIAL":
                material["pending"] += 1
            elif occurrence and occurrence.evidence.get("readback"):
                material["matched"] += 1
            elif status in {"PARTIAL", "UNKNOWN"}:
                # 部分成功仍保留平台回读/缺口证据，不能回退成“等待素材”。
                if occurrence and occurrence.evidence.get("readback"):
                    material["matched"] += 1
                else:
                    material["unknown"] += 1
        else:
            material["not_requested"] += 1
        plan_public.append(
            {
                "id": str(row.id),
                "title": row.title,
                "position": row.position,
                "scheduled_time": row.scheduled_time,
                "order_materials": row.order_materials,
                "external_drama_id": row.external_drama_id,
                "occurrence_id": str(occurrence.id) if occurrence else None,
                "status": status,
            }
        )
        if occurrence:
            build_outcomes.append(
                {
                    "id": str(occurrence.id),
                    "plan_row_id": str(row.id),
                    "status": occurrence.status,
                    "scheduled_at": occurrence.scheduled_at,
                    "preview_id": str(occurrence.preview_id)
                    if occurrence.preview_id
                    else None,
                    "submission_id": str(occurrence.submission_id)
                    if occurrence.submission_id
                    else None,
                    "error_code": occurrence.error_code,
                    "readback": occurrence.evidence.get("readback", {}),
                    "evidence": occurrence.evidence,
                }
            )

    actions = [
        {
            "id": str(row.id),
            "rule_id": str(row.rule_id) if row.rule_id else None,
            "rule_version": row.rule_version,
            "hits": row.hits,
            "action_ledger_refs": row.action_ledger_refs,
            "management_tasks": row.management_tasks,
            "readback_status": row.readback_status,
            "evidence_id": str(row.id),
        }
        for row in day_evidence
    ]
    # 下一触发只显示当前优化师仍然启用的规则绑定；同租户其他优化师的规则
    # 不能因为共享租户而泄露到本优化师的报告。
    bound_rule_ids = session.exec(
        select(AIOptimizerRuleBinding.rule_id).where(
            AIOptimizerRuleBinding.tenant_id == context.tenant_id,
            AIOptimizerRuleBinding.optimizer_id == optimizer_id,
            AIOptimizerRuleBinding.active.is_(True),
        )
    ).all()
    rules = (
        session.exec(
            select(AutomationRule).where(
                AutomationRule.tenant_id == context.tenant_id,
                col(AutomationRule.id).in_(bound_rule_ids),
            )
        ).all()
        if bound_rule_ids
        else []
    )
    current = now or datetime.now(UTC)
    report = {
        "tenant_id": str(context.tenant_id),
        "optimizer_id": str(optimizer_id),
        "optimizer_version": version.number,
        "business_date": business_date,
        "optimization_mode": version.optimization_mode,
        "target_progress": target_counts,
        "schedule_rows": plan_public,
        "material": material,
        "build_outcomes": build_outcomes,
        "rule_actions": actions,
        "pending_proposals": pending,
        "frozen_evidence_ids": sorted(frozen_ids),
        "next_trigger": _next_trigger(
            occurrences=occurrences, rules=rules, now=current
        ),
        "generated_at": current,
    }
    return report


get_daily_report = build_daily_report
