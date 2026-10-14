"""固定时间汇总发送：到点把该时段等待汇总的运行合并成一张飞书卡片。"""

from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.automation_rules import feishu
from app.modules.automation_rules.models import (
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
    AutomationTenantSetting,
)
from app.modules.automation_rules.runner import _execution_lines, _metric_name
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.settings_service import (
    load_feishu,
    load_tenant_setting,
)

SUMMARY_TASK = "automation.send_summary"
register_dispatch_task(SUMMARY_TASK, "control")
# 汇总卡片中合并的执行计数。
_COUNTS = (
    "submitted",
    "skipped_frequency",
    "conflicts",
    "skipped_daily_limit",
    "no_change",
    "unsupported",
    "failed",
)


def due_slot(times: tuple[time, ...], timezone: str, now: datetime) -> datetime | None:
    """租户时区下不晚于 now 的最近一个汇总时刻（UTC）；按当地墙钟计算，跨夏令时不漂移。"""
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    candidates = [
        datetime.combine(day, moment, zone).astimezone(UTC)
        for day in (today, today - timedelta(days=1))
        for moment in times
    ]
    past = [slot for slot in candidates if slot <= now]
    return max(past) if past else None


def scan_summaries(session: Session, *, now: datetime) -> int:
    """为到点的汇总规则入队发送任务；同一汇总时刻只入队一次。

    与规则调度一致：部署开关关闭、规则停用或租户暂停时不发送。
    """
    if not settings.AUTOMATED_RULES_ENABLED:
        return 0
    paused = select(AutomationTenantSetting.tenant_id).where(
        col(AutomationTenantSetting.paused).is_(True)
    )
    rules = session.exec(
        select(AutomationRule)
        .where(
            col(AutomationRule.enabled).is_(True),
            col(AutomationRule.tenant_id).not_in(paused),
            col(AutomationRule.definition)["notification"]["timing"].astext
            == "SUMMARY",
        )
        .with_for_update(skip_locked=True)
    ).all()
    zones: dict[UUID, str] = {}
    count = 0
    for rule in rules:
        try:
            definition = RuleDefinition.model_validate(rule.definition)
        except ValidationError:
            # 定义失效的规则由规则调度停用；这里跳过，不阻塞其他规则的汇总。
            continue
        if not definition.notification.enabled:
            continue
        if rule.tenant_id not in zones:
            zones[rule.tenant_id] = load_tenant_setting(
                session, rule.tenant_id
            ).schedule_timezone
        slot = due_slot(
            definition.notification.summary_times, zones[rule.tenant_id], now
        )
        if slot is None:
            continue
        if rule.last_summary_at is None:
            # 刚改为汇总模式：从最近的时刻开始计，不补发之前的时段。
            rule.last_summary_at = slot
            continue
        if rule.last_summary_at >= slot:
            continue
        rule.last_summary_at = slot
        enqueue_after_commit(
            session,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
            task_name=SUMMARY_TASK,
            task_key=f"automation-summary:{rule.id}:{slot.isoformat()}",
            payload={"rule_id": str(rule.id), "slot": slot.isoformat()},
        )
        count += 1
    session.flush()
    return count


def build_summary_card(
    rule: AutomationRule,
    runs: list[AutomationRuleRun],
    hits: list[AutomationRuleHit],
    timezone: str,
) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    start = runs[0].slot_at.astimezone(zone).strftime("%m-%d %H:%M")
    end = runs[-1].slot_at.astimezone(zone).strftime("%m-%d %H:%M")
    lines = [
        f"{start} ~ {end}（{timezone}）运行 {len(runs)} 次，"
        f"命中合计 {sum(run.hit_count for run in runs)} 次"
    ]
    executed = [
        run
        for run in runs
        if run.execution_status in ("SUBMITTED", "PARTIAL", "FAILED", "SKIPPED")
    ]
    if executed:
        totals: Counter[str] = Counter()
        conflicts: list[dict[str, Any]] = []
        failed_bcs: list[dict[str, Any]] = []
        for run in executed:
            for key in _COUNTS:
                totals[key] += int(run.execution_summary.get(key, 0) or 0)
            conflicts.extend(run.execution_summary.get("conflict_details") or [])
            failed_bcs.extend(run.execution_summary.get("failed_bcs") or [])
        lines.append(f"执行 {len(executed)} 次")
        lines.extend(
            _execution_lines(
                {**totals, "conflict_details": conflicts, "failed_bcs": failed_bcs}
            )
        )
    skipped = {item["advertiser_id"] for run in runs for item in run.skipped_accounts}
    if skipped:
        lines.append(f"{len(skipped)} 个账户数据未更新，已跳过")
    # 同一对象在多次运行中命中，只列最近一次。
    latest: dict[str, AutomationRuleHit] = {}
    for hit in hits:
        latest[hit.subject_key] = hit
    ordered = sorted(latest.values(), key=lambda hit: hit.sequence)
    card_hits = [
        feishu.CardHit(
            label=hit.subject_label,
            drama=hit.drama_name if hit.drama_name != hit.subject_label else None,
            metrics=[(_metric_name(k), v or "—") for k, v in hit.metrics.items()],
            target_count=hit.target_count,
        )
        for hit in ordered[: feishu.MAX_CARD_HITS]
    ]
    link = (
        f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{rule.tenant_id}"
        "/automation-rules?tab=runs"
    )
    return feishu.build_card(
        title=f"【汇总】{rule.name}",
        rule_name=rule.name,
        summary_lines=lines,
        hits=card_hits,
        total_hits=len(latest),
        link=link,
        template="orange" if latest else "blue",
    )


def deliver_summary(
    session: Session,
    *,
    rule_id: UUID,
    slot: datetime,
    transport: httpx.BaseTransport | None = None,
) -> str:
    rule = session.get(AutomationRule, rule_id)
    if rule is None:
        return "RULE_DELETED"
    runs = list(
        session.exec(
            select(AutomationRuleRun)
            .where(
                AutomationRuleRun.rule_id == rule.id,
                AutomationRuleRun.notify_status == "SUMMARY",
                col(AutomationRuleRun.slot_at) <= slot,
            )
            .order_by(col(AutomationRuleRun.slot_at))
            .with_for_update()
        ).all()
    )
    if not runs:
        return "NOT_REQUIRED"
    hits = list(
        session.exec(
            select(AutomationRuleHit)
            .join(
                AutomationRuleRun,
                col(AutomationRuleRun.id) == AutomationRuleHit.run_id,
            )
            .where(col(AutomationRuleHit.run_id).in_([run.id for run in runs]))
            .order_by(col(AutomationRuleRun.slot_at), col(AutomationRuleHit.sequence))
        ).all()
    )
    target = load_feishu(session, rule.tenant_id)
    status, error = "SENT", None
    if target is None:
        status, error = "FAILED", "未配置或未启用飞书通知"
    else:
        timezone = load_tenant_setting(session, rule.tenant_id).schedule_timezone
        try:
            feishu.send(
                target[0],
                target[1],
                build_summary_card(rule, runs, hits, timezone),
                transport=transport,
            )
        except DomainError as exc:
            status, error = "FAILED", str(exc.message)[:500]
    for run in runs:
        run.notify_status, run.notify_error = status, error
    session.flush()
    return status
