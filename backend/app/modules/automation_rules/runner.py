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
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import (
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
    try:
        # 后台运行以规则最后修改人的身份重建权限；此人已离开租户时规则不再运行。
        require_tenant(
            session, actor_id=rule.updated_by, tenant_id=rule.tenant_id, action="read"
        )
    except DomainError:
        return _finish_failed(run, "rule_owner_forbidden", now)
    try:
        definition = RuleDefinition.model_validate(rule.definition)
        inputs = load_inputs(
            session, tenant_id=rule.tenant_id, definition=definition, now=now
        )
        result = evaluate(definition, inputs)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception(
            "automation rule evaluation failed", extra={"rule_id": str(rule.id)}
        )
        return _finish_failed(run, "evaluation_failed", now)
    for sequence, hit in enumerate(result.hits):
        session.add(
            AutomationRuleHit(
                tenant_id=rule.tenant_id,
                run_id=run.id,
                sequence=sequence,
                subject_key=hit.subject_key,
                subject_label=hit.subject_label[:500],
                drama_name=hit.drama,
                advertiser_id=hit.advertiser_id,
                bc_id=hit.bc_id,
                currency=hit.currency,
                metrics=hit.metrics,
                targets=[asdict(target) for target in hit.targets],
                outcome="NOTIFIED",
            )
        )
    run.checked_count = result.checked
    run.hit_count = len(result.hits)
    run.skipped_count = result.skipped_count
    run.skipped_accounts = inputs.skipped_accounts
    run.status, run.finished_at = "COMPLETE", now
    rule.last_run_at = now
    notify = definition.notification
    if notify.enabled and (
        result.hits or notify.notify_without_hits or inputs.skipped_accounts
    ):
        run.notify_status = "PENDING"
        enqueue_after_commit(
            session,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
            task_name=SEND_TASK,
            task_key=f"automation-feishu:{run.id}",
            payload={"run_id": str(run.id)},
        )
    session.flush()
    return run


def _metric_name(key: str) -> str:
    metric, _, range_type = key.partition(":")
    return f"{RANGE_LABELS.get(range_type, '')}{metric_label(metric)}"


def build_run_card(
    rule: AutomationRule, run: AutomationRuleRun, hits: list[AutomationRuleHit]
) -> dict[str, Any]:
    kind = "预警" if run.hit_count else "运行报告"
    title = f"【{kind}】{rule.name}"
    summary = [f"判断对象 {run.checked_count} 个，命中 {run.hit_count} 个"]
    if run.skipped_accounts:
        summary.append(f"{len(run.skipped_accounts)} 个账户数据未更新，已跳过")
    if run.skipped_count:
        summary.append(f"{run.skipped_count} 个对象命名不规范，未参与剧合计")
    card_hits = [
        feishu.CardHit(
            label=hit.subject_label,
            drama=hit.drama_name if hit.drama_name != hit.subject_label else None,
            metrics=[(_metric_name(k), v or "—") for k, v in hit.metrics.items()],
            target_count=len(hit.targets),
        )
        for hit in hits[: feishu.MAX_CARD_HITS]
    ]
    link = (
        f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{rule.tenant_id}"
        f"/automation-rules?tab=runs&run={run.id}"
    )
    return feishu.build_card(
        title=title,
        rule_name=rule.name,
        summary_lines=summary,
        hits=card_hits,
        total_hits=run.hit_count,
        link=link,
        template="orange" if run.hit_count else "blue",
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
        feishu.send(
            target[0], target[1], build_run_card(rule, run, hits), transport=transport
        )
    except DomainError as exc:
        run.notify_status, run.notify_error = "FAILED", str(exc.message)[:500]
    else:
        run.notify_status, run.notify_error = "SENT", None
    session.flush()
    return run.notify_status
