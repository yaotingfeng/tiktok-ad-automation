"""执行规则命中：频控、冷却期冲突、每日涨幅上限，再经广告管理的冻结预览与任务提交执行。

不直接调用 TikTok：写入、重试、写前观测核对与写后定向刷新全部由广告管理负责。
"""

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.submissions import submit_management_task
from app.modules.automation_rules.actions import (
    action_key,
    frequency_allows,
    group_by_bc,
    mutation_for,
    opposite,
    selection_payload,
    target_index,
    use_key,
)
from app.modules.automation_rules.models import (
    AutomationActionLedger,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.settings_service import load_tenant_setting
from app.modules.reporting.query_models import FrozenSelectionRecord
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)

MANAGEMENT_SOURCE = "automation_rule"
SELECTION_TTL = timedelta(minutes=15)


def _summary() -> dict[str, Any]:
    return {
        "submitted": 0,
        "skipped_frequency": 0,
        "skipped_cooldown": 0,
        "skipped_daily_limit": 0,
        "conflicts": 0,
        "no_change": 0,
        "failed": 0,
        "failed_bcs": [],
        "tasks": [],
        "conflict_details": [],
    }


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation, ValueError:
        return None


def lock_tenant(session: Session, tenant_id: UUID) -> None:
    # 同一租户的执行串行化：后到的执行一定能看到先到者写下的台账，冲突判断才可靠。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"automation-exec:{tenant_id}"},
    )


def _selection(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    targets: list[dict[str, Any]],
    run: AutomationRuleRun,
    now: datetime,
) -> FrozenSelectionRecord:
    """规则命中直接写成一条冻结选择，复用广告管理的预览与提交校验。"""
    refs, uses = selection_payload(targets, context.tenant_id)
    filters = {
        "source": MANAGEMENT_SOURCE,
        "rule_id": str(run.rule_id),
        "run_id": str(run.id),
    }
    digest_source = {
        "refs": [
            (r["tenant_id"], r["advertiser_id"], r["kind"], r["remote_id"])
            for r in refs
        ],
        "uses": [
            (
                u["ad_ref"]["tenant_id"],
                u["ad_ref"]["advertiser_id"],
                u["ad_ref"]["remote_id"],
                u["platform_material_id"],
                u["ad_material_id"],
                u["material_type"],
            )
            for u in uses
        ],
    }
    record = FrozenSelectionRecord(
        tenant_id=context.tenant_id,
        bc_id=bc_id,
        actor_id=context.actor_id,
        created_at=now,
        expires_at=max(now, datetime.now(UTC)) + SELECTION_TTL,
        snapshot_id=uuid4(),
        advertiser_ids=sorted({t["advertiser_id"] for t in targets}),
        filters=filters,
        filter_digest=hashlib.sha256(
            json.dumps(filters, sort_keys=True).encode()
        ).hexdigest(),
        publication_versions={},
        naming_versions={},
        refs=refs,
        material_uses=uses,
        membership_digest=hashlib.sha256(
            json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    session.add(record)
    session.flush()
    return record


def _target_key(
    index: dict[tuple[Any, ...], str], ref: EntityRef, material_use: Any
) -> str:
    if material_use is not None:
        found = index.get(
            (
                "use",
                *use_key(
                    material_use.ad_ref.advertiser_id,
                    material_use.ad_ref.remote_id,
                    material_use.platform_material_id,
                    material_use.ad_material_id,
                    material_use.material_type,
                ),
            )
        )
        if found:
            return found
        return f"material:{ref.advertiser_id}:{material_use.platform_material_id}"
    # 系列或广告组被展开到下级（如预算归属的系列）时，没有对应规则目标，按对象身份记账。
    return index.get(
        ("object", ref.advertiser_id, ref.kind, ref.remote_id),
        f"{ref.kind}:{ref.advertiser_id}:{ref.remote_id}",
    )


def _day_start(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone)).date()
    return datetime.combine(local, time(0), ZoneInfo(timezone)).astimezone(UTC)


def execute_hits(
    session: Session,
    *,
    run: AutomationRuleRun,
    rule: AutomationRule,
    selected_keys: set[str] | None,
    now: datetime,
    actor_id: UUID,
    require_manage: bool,
) -> dict[str, Any]:
    """以 actor_id 身份执行：自动执行为规则最后修改人（须仍是租户管理员），
    人工确认为确认人；管理任务与审计记录都归属于该身份。"""
    summary = _summary()
    lock_tenant(session, rule.tenant_id)
    context = TenantContext(rule.tenant_id, actor_id, "operator")
    try:
        require_tenant(
            session,
            actor_id=actor_id,
            tenant_id=rule.tenant_id,
            action="ads_manage",
        )
        if require_manage:
            require_tenant(
                session,
                actor_id=actor_id,
                tenant_id=rule.tenant_id,
                action="manage",
            )
    except DomainError:
        summary["error"] = "rule_owner_forbidden"
        run.execution_status, run.execution_summary = "FAILED", summary
        session.flush()
        return summary
    definition = RuleDefinition.model_validate(run.definition)
    action = definition.action
    key = action_key(action)
    setting = load_tenant_setting(session, rule.tenant_id)
    hits = list(
        session.exec(
            select(AutomationRuleHit)
            .where(AutomationRuleHit.run_id == run.id)
            .order_by(col(AutomationRuleHit.sequence))
        ).all()
    )
    # 每个执行对象记住来自哪些命中，结果回写到命中上。
    owners: dict[str, list[AutomationRuleHit]] = defaultdict(list)
    targets: dict[str, dict[str, Any]] = {}
    for hit in hits:
        if selected_keys is not None and hit.subject_key not in selected_keys:
            hit.outcome = "NOT_SELECTED"
            continue
        for target in hit.targets:
            targets.setdefault(target["key"], target)
            owners[target["key"]].append(hit)
    outcome: dict[str, str] = {}
    history = _ledger(
        session, rule.tenant_id, list(targets), now, setting.reverse_cooldown_hours
    )
    rule_names = _rule_names(session, {row.rule_id for row in history})
    runnable: dict[str, dict[str, Any]] = {}
    for target_key, target in targets.items():
        rows = [row for row in history if row.target_key == target_key]
        conflict = next(
            (
                row
                for row in rows
                if row.rule_id != rule.id
                and row.created_at
                >= now - timedelta(hours=setting.reverse_cooldown_hours)
                and opposite(key, row.action)
            ),
            None,
        )
        if conflict is not None:
            outcome[target_key] = "CONFLICT"
            summary["conflicts"] += 1
            summary["conflict_details"].append(
                {
                    "target_key": target_key,
                    "rule_name": rule_names.get(conflict.rule_id, ""),
                    "action": conflict.action,
                }
            )
            continue
        own = [
            row.created_at
            for row in rows
            if row.rule_id == rule.id and row.action == key
        ]
        if not frequency_allows(action.frequency, own, now):
            outcome[target_key] = "SKIPPED_FREQUENCY"
            summary["skipped_frequency"] += 1
            continue
        runnable[target_key] = target
    day_start = _day_start(now, setting.schedule_timezone)
    for bc_id, bc_targets in group_by_bc(runnable.values()).items():
        # 保存点回滚时，本 BC 已计入的计数与结果一并撤销，整个 BC 记为失败。
        counts = {
            key: value for key, value in summary.items() if isinstance(value, int)
        }
        before = dict(outcome)
        try:
            with session.begin_nested():
                submitted = _execute_bc(
                    session,
                    context=context,
                    run=run,
                    rule=rule,
                    bc_id=bc_id,
                    targets=bc_targets,
                    action_name=key,
                    history=history,
                    day_start=day_start,
                    limit_pct=setting.daily_increase_limit_pct,
                    now=now,
                    outcome=outcome,
                    summary=summary,
                )
                if submitted:
                    summary["tasks"].append({"bc_id": bc_id, "task_id": str(submitted)})
        except Exception as exc:
            if isinstance(exc, DomainError):
                reason = exc.code
            elif isinstance(exc, HTTPException):
                reason = str(exc.detail)
            else:
                reason = "unexpected_error"
                logger.exception(
                    "automation execution crashed for bc",
                    extra={"rule_id": str(rule.id), "bc_id": bc_id},
                )
            logger.warning(
                "automation execution failed for bc",
                extra={"rule_id": str(rule.id), "bc_id": bc_id, "reason": reason},
            )
            summary.update(counts)
            outcome.clear()
            outcome.update(before)
            summary["failed_bcs"].append({"bc_id": bc_id, "reason": reason})
            for target in bc_targets:
                outcome[target["key"]] = "FAILED"
                summary["failed"] += 1
    for target_key, value in outcome.items():
        for hit in owners[target_key]:
            # 一个命中涉及多个对象时，按最需要关注的结果展示。
            if _priority(value) > _priority(hit.outcome):
                hit.outcome = value
    if summary["submitted"] == 0:
        # 没有提交任何变更：有失败记为失败，否则全部被频控、冷却期等跳过。
        run.execution_status = "FAILED" if summary["failed"] else "SKIPPED"
    elif summary["failed"] or summary["conflicts"] or summary["skipped_daily_limit"]:
        run.execution_status = "PARTIAL"
    else:
        run.execution_status = "SUBMITTED"
    run.execution_summary = summary
    session.flush()
    return summary


_PRIORITY = [
    "NOTIFIED",
    "PENDING_CONFIRMATION",
    "NOT_SELECTED",
    "NO_CHANGE",
    "SKIPPED_FREQUENCY",
    "SKIPPED_DAILY_LIMIT",
    "SUBMITTED",
    "CONFLICT",
    "FAILED",
]


def _priority(value: str) -> int:
    return _PRIORITY.index(value) if value in _PRIORITY else -1


def _ledger(
    session: Session,
    tenant_id: UUID,
    keys: list[str],
    now: datetime,
    cooldown_hours: int,
) -> list[AutomationActionLedger]:
    if not keys:
        return []
    # 频控、冷却期与当日涨幅都只需要最近的台账：冷却期最长 168 小时，
    # 频控最长一周，自定义频控按天计最多 30 天。
    since = now - timedelta(days=31, hours=cooldown_hours)
    return list(
        session.exec(
            select(AutomationActionLedger)
            .where(
                AutomationActionLedger.tenant_id == tenant_id,
                col(AutomationActionLedger.target_key).in_(keys),
                col(AutomationActionLedger.created_at) >= since,
            )
            .order_by(col(AutomationActionLedger.created_at))
        ).all()
    )


def _rule_names(session: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = session.exec(
        select(AutomationRule.id, AutomationRule.name).where(
            col(AutomationRule.id).in_(list(ids))
        )
    ).all()
    return dict(rows)


def _execute_bc(
    session: Session,
    *,
    context: TenantContext,
    run: AutomationRuleRun,
    rule: AutomationRule,
    bc_id: str,
    targets: list[dict[str, Any]],
    action_name: str,
    history: list[AutomationActionLedger],
    day_start: datetime,
    limit_pct: int,
    now: datetime,
    outcome: dict[str, str],
    summary: dict[str, Any],
) -> UUID | None:
    definition = RuleDefinition.model_validate(run.definition)
    selection = _selection(
        session, context=context, bc_id=bc_id, targets=targets, run=run, now=now
    )
    mutation = mutation_for(definition.action)
    index = target_index(targets)
    preview = prepare_preview(session, context, bc_id, selection.id, mutation)
    field_prefix = action_name.partition("_")[0] + "_"
    if action_name.startswith(("BUDGET_", "ROAS_")):
        # 当日累计涨幅：任何把值调高的执行（提高或调整至）都受限，
        # 基准是当日该字段第一次被规则调整前的值。
        excluded = []
        for item in preview.items:
            if item.execution_result != "PENDING" or item.original_value is None:
                continue
            final = item.final_value
            if not isinstance(final, Decimal) or final <= item.original_value:
                continue
            target_key = _target_key(index, item.ref, item.material_use)
            today = [
                row
                for row in history
                if row.target_key == target_key
                and row.action.startswith(field_prefix)
                and row.created_at >= day_start
            ]
            first = _decimal(today[0].original_value) if today else None
            baseline = first if first is not None else item.original_value
            if final > baseline * (Decimal(1) + Decimal(limit_pct) / 100):
                excluded.append(item.ref)
                outcome[target_key] = "SKIPPED_DAILY_LIMIT"
                summary["skipped_daily_limit"] += 1
        if excluded:
            mutation = mutation.model_copy(update={"excluded_refs": tuple(excluded)})
            preview = prepare_preview(session, context, bc_id, selection.id, mutation)
    pending = []
    for item in preview.items:
        target_key = _target_key(index, item.ref, item.material_use)
        if target_key in outcome:
            continue
        if item.execution_result == "PENDING":
            pending.append((target_key, item))
        elif item.execution_result == "NO_CHANGE":
            outcome[target_key] = "NO_CHANGE"
            summary["no_change"] += 1
        else:
            outcome[target_key] = "FAILED"
            summary["failed"] += 1
    for target in targets:
        if target["key"] not in outcome and not any(
            k == target["key"] for k, _ in pending
        ):
            # 预览没有为它生成可执行项（例如目录中不存在），视为失败而不是静默跳过。
            outcome[target["key"]] = "FAILED"
            summary["failed"] += 1
    if not pending:
        return None
    task = submit_management_task(
        session,
        context,
        preview.preview_id,
        preview.digest,
        uuid5(NAMESPACE_URL, f"automation:{run.id}:{bc_id}"),
    )
    seen: set[str] = set()
    for target_key, item in pending:
        if target_key not in seen:
            seen.add(target_key)
            outcome[target_key] = "SUBMITTED"
            summary["submitted"] += 1
        session.add(
            AutomationActionLedger(
                tenant_id=rule.tenant_id,
                rule_id=rule.id,
                run_id=run.id,
                target_key=target_key,
                action=action_name,
                original_value=None
                if item.original_value is None
                else str(item.original_value),
                final_value=None if item.final_value is None else str(item.final_value),
                management_task_id=task.task_id,
                created_at=now,
            )
        )
    session.flush()
    return task.task_id
