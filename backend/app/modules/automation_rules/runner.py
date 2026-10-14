"""一次规则运行：判断、持久化命中、按需投递飞书通知。阶段 1 只预警，不改广告。"""

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.automation_rules import feishu
from app.modules.automation_rules.confirmations import (
    EXECUTE_TASK,
    expire_open,
    open_batch,
)
from app.modules.automation_rules.evaluate import Evaluation, evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleHit,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.service import metric_label
from app.modules.automation_rules.settings_service import load_feishu
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)

RANGE_LABELS = {
    "TODAY": "今天",
    "YESTERDAY": "昨天",
    "PAST_3_DAYS": "近 3 天",
    "PAST_5_DAYS": "近 5 天",
    "PAST_7_DAYS": "近 7 天",
    "LIFETIME": "投放以来",
}
MAX_CONFLICT_LINES = 5
SEND_TASK = "automation.send_feishu"
# 投递方在入队前就要能解析队列，注册放在投递所在模块。
register_dispatch_task(SEND_TASK, "control")


def _existing(
    session: Session, rule_id: UUID, slot_at: datetime
) -> AutomationRuleRun | None:
    return session.exec(
        select(AutomationRuleRun).where(
            AutomationRuleRun.rule_id == rule_id, AutomationRuleRun.slot_at == slot_at
        )
    ).first()


def _start(
    session: Session, rule: AutomationRule, slot_at: datetime, now: datetime
) -> AutomationRuleRun | None:
    """同一槽位只允许一次运行；重复投递时返回 None 交由调用方读取已有运行。"""
    run = AutomationRuleRun(
        tenant_id=rule.tenant_id,
        rule_id=rule.id,
        rule_version=rule.version,
        definition=rule.definition,
        slot_at=slot_at,
        started_at=now,
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        return None
    return run


def _finish_failed(
    run: AutomationRuleRun, code: str, now: datetime
) -> AutomationRuleRun:
    run.status, run.error_code, run.finished_at = "FAILED", code, now
    return run


def _write_hits(
    session: Session, rule: AutomationRule, run: AutomationRuleRun, result: Evaluation
) -> None:
    for sequence, hit in enumerate(result.hits):
        session.add(
            AutomationRuleHit(
                tenant_id=rule.tenant_id,
                run_id=run.id,
                sequence=sequence,
                # 剧名与对象键来自平台命名，长度不受控，按列宽截断。
                subject_key=hit.subject_key[:300],
                subject_label=hit.subject_label[:500],
                drama_name=hit.drama[:300] if hit.drama else None,
                advertiser_id=hit.advertiser_id,
                bc_id=hit.bc_id,
                currency=hit.currency,
                metrics=hit.metrics,
                targets=[asdict(target) for target in hit.targets],
                target_count=hit.target_count,
                outcome="NOTIFIED",
            )
        )
    session.flush()


def execute_run(
    session: Session, *, rule: AutomationRule, slot_at: datetime, now: datetime
) -> AutomationRuleRun:
    existing = _existing(session, rule.id, slot_at)
    if existing is not None:
        return existing
    run = _start(session, rule, slot_at, now)
    if run is None:
        found = _existing(session, rule.id, slot_at)
        assert found is not None
        return found
    # 新一轮运行开始，上一轮仍未确认的批次失效。
    expire_open(session, rule_id=rule.id, now=now)
    try:
        # 后台运行以规则最后修改人的身份重建权限；此人已离开租户时规则不再运行。
        require_tenant(
            session, actor_id=rule.updated_by, tenant_id=rule.tenant_id, action="read"
        )
    except DomainError:
        return _finish_failed(run, "rule_owner_forbidden", now)
    # 判断与命中写入放在保存点里：数据库错误或超时只回滚这一部分，
    # 运行记录仍以失败状态提交，槽位不会无记录地丢失。
    try:
        with session.begin_nested():
            definition = RuleDefinition.model_validate(rule.definition)
            inputs = load_inputs(
                session, tenant_id=rule.tenant_id, definition=definition, now=now
            )
            result = evaluate(definition, inputs)
            _write_hits(session, rule, run, result)
    except SoftTimeLimitExceeded:
        logger.warning("automation rule timed out", extra={"rule_id": str(rule.id)})
        return _finish_failed(run, "evaluation_timeout", now)
    except Exception:
        logger.exception(
            "automation rule evaluation failed", extra={"rule_id": str(rule.id)}
        )
        return _finish_failed(run, "evaluation_failed", now)
    run.checked_count = result.checked
    run.hit_count = len(result.hits)
    run.skipped_count = result.skipped_count
    run.skipped_accounts = inputs.skipped_accounts
    run.status, run.finished_at = "COMPLETE", now
    run.mode = definition.mode
    rule.last_run_at = now
    context = TenantContext(rule.tenant_id, rule.updated_by, "operator")
    total_targets = sum(hit.target_count for hit in result.hits)
    if (
        result.hits
        and definition.mode == "AUTO"
        and total_targets <= definition.max_targets
    ):
        # 自动执行：交给广告管理队列执行，执行完成后再发结果卡片。
        run.execution_status = "QUEUED"
        enqueue_after_commit(
            session,
            context=context,
            task_name=EXECUTE_TASK,
            task_key=f"automation-exec:{run.id}",
            payload={"run_id": str(run.id), "mode": "AUTO"},
        )
        session.flush()
        return run
    if result.hits and definition.mode != "NOTIFY":
        # 确认后执行，或自动执行命中对象超过单次上限：先生成待确认批次，由人确认。
        open_batch(session, run=run, rule=rule, now=now)
        if definition.mode == "AUTO":
            run.execution_summary = {
                "reason": "max_targets_exceeded",
                "target_count": total_targets,
                "max_targets": definition.max_targets,
            }
    notify = definition.notification
    if notify.enabled and (
        result.hits or notify.notify_without_hits or inputs.skipped_accounts
    ):
        queue_notification(session, run=run, context=context)
    session.flush()
    return run


def queue_notification(
    session: Session, *, run: AutomationRuleRun, context: TenantContext
) -> None:
    run.notify_status = "PENDING"
    enqueue_after_commit(
        session,
        context=context,
        task_name=SEND_TASK,
        # 同一次运行可能先后发送待确认与执行结果两张卡片，按状态区分任务标识。
        task_key=f"automation-feishu:{run.id}:{run.execution_status}",
        payload={"run_id": str(run.id)},
    )


def notify_execution(
    session: Session, *, run: AutomationRuleRun, rule: AutomationRule
) -> None:
    """执行完成后按运行时的规则快照决定是否发执行结果卡片。"""
    definition = RuleDefinition.model_validate(run.definition)
    if definition.notification.enabled:
        queue_notification(
            session,
            run=run,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
        )
    session.flush()


def _metric_name(key: str) -> str:
    metric, _, range_type = key.partition(":")
    return f"{RANGE_LABELS.get(range_type, '')}{metric_label(metric)}"


def _execution_lines(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"提交 {summary.get('submitted', 0)} 个对象；"
        f"频控跳过 {summary.get('skipped_frequency', 0)}、"
        f"冷却期冲突 {summary.get('conflicts', 0)}、"
        f"超出每日涨幅 {summary.get('skipped_daily_limit', 0)}、"
        f"无需变更 {summary.get('no_change', 0)}"
    ]
    if summary.get("failed"):
        lines.append(f"失败 {summary['failed']} 个对象")
    if summary.get("error"):
        lines.append("规则最后修改人已无执行权限，未执行")
    failed_bcs = summary.get("failed_bcs") or []
    if failed_bcs:
        reasons = "、".join(f"{bc['bc_id']}：{bc['reason']}" for bc in failed_bcs)
        lines.append(f"失败的 BC：{reasons}")
    details = summary.get("conflict_details") or []
    for detail in details[:MAX_CONFLICT_LINES]:
        lines.append(
            f"冲突：{detail['target_key']} 与规则「{detail['rule_name']}」相反"
        )
    if len(details) > MAX_CONFLICT_LINES:
        lines.append(f"其余 {len(details) - MAX_CONFLICT_LINES} 个冲突请在 TK-ADA 查看")
    return lines


def build_run_card(
    rule: AutomationRule,
    run: AutomationRuleRun,
    hits: list[AutomationRuleHit],
    *,
    confirmation_id: UUID | None = None,
) -> dict[str, Any]:
    """卡片按运行状态分三类:待确认、执行结果、预警/运行报告。"""
    summary = [f"判断对象 {run.checked_count} 个，命中 {run.hit_count} 个"]
    if run.skipped_accounts:
        summary.append(f"{len(run.skipped_accounts)} 个账户数据未更新，已跳过")
    if run.skipped_count:
        summary.append(f"{run.skipped_count} 个对象命名不规范，未参与剧合计")
    base = f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{rule.tenant_id}"
    link = f"{base}/automation-rules?tab=runs&run={run.id}"
    button = "查看明细"
    status = run.execution_status
    if status == "AWAITING_CONFIRMATION":
        title, template = f"【待确认】{rule.name}", "yellow"
        if run.execution_summary.get("reason") == "max_targets_exceeded":
            summary.append(
                f"涉及 {run.execution_summary.get('target_count')} 个对象，"
                f"超过单次自动执行上限 {run.execution_summary.get('max_targets')}，"
                "已转为人工确认"
            )
        # 下一轮运行开始时未处理的批次会失效。
        summary.append("请在规则下次运行前确认，过期后需等待新的判断结果")
        if confirmation_id is not None:
            link = (
                f"{base}/automation-rules?tab=confirmations"
                f"&confirmation={confirmation_id}"
            )
            button = "去确认"
    elif status in ("SUBMITTED", "PARTIAL", "FAILED"):
        failed = status == "FAILED" or bool(run.execution_summary.get("failed_bcs"))
        title, template = f"【已执行】{rule.name}", "red" if failed else "green"
        summary.extend(_execution_lines(run.execution_summary))
    else:
        kind = "预警" if run.hit_count else "运行报告"
        title = f"【{kind}】{rule.name}"
        template = "orange" if run.hit_count else "blue"
    card_hits = [
        feishu.CardHit(
            label=hit.subject_label,
            drama=hit.drama_name if hit.drama_name != hit.subject_label else None,
            metrics=[(_metric_name(k), v or "—") for k, v in hit.metrics.items()],
            target_count=hit.target_count,
        )
        for hit in hits[: feishu.MAX_CARD_HITS]
    ]
    return feishu.build_card(
        title=title,
        rule_name=rule.name,
        summary_lines=summary,
        hits=card_hits,
        total_hits=run.hit_count,
        link=link,
        template=template,
        button=button,
    )


def deliver_notification(
    session: Session,
    *,
    run_id: UUID,
    transport: httpx.BaseTransport | None = None,
) -> str:
    run = session.get(AutomationRuleRun, run_id)
    if run is None or run.notify_status != "PENDING":
        return run.notify_status if run else "NOT_REQUIRED"
    rule = session.get(AutomationRule, run.rule_id)
    target = load_feishu(session, run.tenant_id)
    if rule is None or target is None:
        run.notify_status, run.notify_error = "FAILED", "未配置或未启用飞书通知"
        session.flush()
        return run.notify_status
    hits = list(
        session.exec(
            select(AutomationRuleHit)
            .where(AutomationRuleHit.run_id == run.id)
            .order_by(col(AutomationRuleHit.sequence))
            .limit(feishu.MAX_CARD_HITS)
        ).all()
    )
    try:
        confirmation_id = session.exec(
            select(AutomationConfirmation.id).where(
                AutomationConfirmation.run_id == run.id,
                AutomationConfirmation.status == "PENDING",
            )
        ).first()
        card = build_run_card(rule, run, hits, confirmation_id=confirmation_id)
        feishu.send(target[0], target[1], card, transport=transport)
    except DomainError as exc:
        run.notify_status, run.notify_error = "FAILED", str(exc.message)[:500]
    else:
        run.notify_status, run.notify_error = "SENT", None
    session.flush()
    return run.notify_status
