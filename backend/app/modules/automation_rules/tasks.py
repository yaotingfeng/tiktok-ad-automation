"""自动规则后台任务：Beat 扫描到期规则，经 outbox 投递运行与飞书发送。"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.automation_rules.executor import execute_hits, lock_tenant
from app.modules.automation_rules.models import (
    AutomationConfirmation,
    AutomationRule,
    AutomationRuleRun,
    AutomationTenantSetting,
)
from app.modules.automation_rules.runner import (
    deliver_notification,
    execute_run,
    notify_execution,
)
from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.settings_service import load_tenant_setting

register_dispatch_task("automation.run_rule", "ads-reporting")

logger = logging.getLogger(__name__)

SCAN_LIMIT = 200
RUN_RETENTION = timedelta(days=30)


def runnable(session: Session, rule: AutomationRule) -> bool:
    """投递后到真正执行之间，规则可能已被关闭、租户暂停或部署开关关闭，执行前重新检查。"""
    if not settings.AUTOMATED_RULES_ENABLED or not rule.enabled:
        return False
    return not load_tenant_setting(session, rule.tenant_id).paused


def scan_due_rules(session: Session, *, now: datetime) -> int:
    """投递到期规则并推进下一次运行时刻；部署开关关闭时不调度任何规则。"""
    if not settings.AUTOMATED_RULES_ENABLED:
        return 0
    # 暂停的租户在 SQL 中排除：保持到期状态，恢复后立即补跑一次；
    # 也不会因为大量暂停规则排在前面而占满扫描上限、饿死其他租户。
    paused = select(AutomationTenantSetting.tenant_id).where(
        col(AutomationTenantSetting.paused).is_(True)
    )
    rules = session.exec(
        select(AutomationRule)
        .where(
            col(AutomationRule.enabled).is_(True),
            col(AutomationRule.next_run_at) <= now,
            col(AutomationRule.tenant_id).not_in(paused),
        )
        .order_by(col(AutomationRule.next_run_at))
        .limit(SCAN_LIMIT)
        .with_for_update(skip_locked=True, of=AutomationRule)
    ).all()
    count = 0
    for rule in rules:
        try:
            definition = RuleDefinition.model_validate(rule.definition)
        except ValidationError:
            # 指标目录调整后旧定义可能不再合法：只停用这一条规则，不影响其他规则调度。
            logger.warning(
                "automation rule definition invalid", extra={"rule_id": str(rule.id)}
            )
            rule.enabled, rule.next_run_at = False, None
            continue
        setting = load_tenant_setting(session, rule.tenant_id)
        slot = rule.next_run_at
        assert slot is not None
        enqueue_after_commit(
            session,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
            task_name="automation.run_rule",
            task_key=f"automation-run:{rule.id}:{slot.isoformat()}",
            payload={"rule_id": str(rule.id), "slot_at": slot.isoformat()},
        )
        if definition.schedule.window == "ONCE":
            rule.enabled, rule.next_run_at = False, None
        else:
            rule.next_run_at = next_run_at(
                definition.schedule, after=now, timezone=setting.schedule_timezone
            )
        count += 1
    session.flush()
    return count


def cleanup_runs(session: Session, *, now: datetime) -> int:
    result = session.connection().execute(
        delete(AutomationRuleRun).where(
            col(AutomationRuleRun.started_at) < now - RUN_RETENTION
        )
    )
    return int(result.rowcount or 0)


def _payload(
    payload: dict[str, Any],
    keys: set[str],
    *,
    optional: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    payload = validate_dispatch_payload(payload)
    if not keys <= set(payload) <= keys | optional:
        raise DomainError("dispatch_payload_invalid", "自动规则任务参数无效")
    return payload


@celery_app.task(name="automation.scan_due", time_limit=60, soft_time_limit=55)
def scan_due() -> int:
    with Session(engine) as session, session.begin():
        return scan_due_rules(session, now=datetime.now(UTC))


@celery_app.task(
    name="automation.run_rule",
    time_limit=300,
    soft_time_limit=280,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_rule(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    del actor_id  # 运行时按规则最后修改人重建权限，不信任投递时的身份。
    data = _payload(payload, {"rule_id", "slot_at"})
    with Session(engine) as session, session.begin():
        rule = session.get(AutomationRule, UUID(data["rule_id"]))
        if rule is None:
            return "RULE_DELETED"
        if str(rule.tenant_id) != tenant_id:
            raise DomainError("action_forbidden", "规则不属于该租户")
        if not runnable(session, rule):
            return "SKIPPED"
        run = execute_run(
            session,
            rule=rule,
            slot_at=datetime.fromisoformat(data["slot_at"]),
            now=datetime.now(UTC),
        )
        return run.status


@celery_app.task(name="automation.send_feishu", time_limit=60, soft_time_limit=50)
def send_feishu(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    del actor_id
    data = _payload(payload, {"run_id"})
    with Session(engine) as session, session.begin():
        run = session.get(AutomationRuleRun, UUID(data["run_id"]))
        if run is None:
            return "RUN_DELETED"
        if str(run.tenant_id) != tenant_id:
            raise DomainError("action_forbidden", "运行不属于该租户")
        # 发送失败已记录在运行上，不自动重试，避免同一条预警重复刷屏。
        return deliver_notification(session, run_id=run.id)


@celery_app.task(name="automation.cleanup", time_limit=120, soft_time_limit=110)
def cleanup() -> int:
    with Session(engine) as session, session.begin():
        return cleanup_runs(session, now=datetime.now(UTC))


@celery_app.task(
    name="automation.execute",
    time_limit=300,
    soft_time_limit=280,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> str:
    """执行一次运行的命中：自动执行，或人确认后执行所选命中。"""
    del actor_id  # 执行身份由运行本身决定：规则最后修改人或确认人。
    data = _payload(payload, {"run_id", "mode"}, optional={"selected_keys"})
    if data["mode"] not in ("AUTO", "CONFIRM"):
        raise DomainError("dispatch_payload_invalid", "自动规则执行模式无效")
    try:
        with Session(engine) as session, session.begin():
            return run_execution(session, tenant_id=tenant_id, data=data)
    except Exception:
        # 执行任务不重试：意外错误时把运行记为失败并发结果卡片，避免永远停在执行中。
        logger.exception("automation execute crashed", extra={"run_id": data["run_id"]})
        with Session(engine) as session, session.begin():
            run = session.get(AutomationRuleRun, UUID(data["run_id"]))
            if run is None or run.execution_status not in ("QUEUED", "CONFIRMED"):
                return run.execution_status if run else "RUN_DELETED"
            _fail(session, run, "execution_error")
        return "FAILED"


def _fail(session: Session, run: AutomationRuleRun, error: str) -> None:
    run.execution_status = "FAILED"
    run.execution_summary = {**run.execution_summary, "error": error}
    rule = session.get(AutomationRule, run.rule_id)
    if rule is not None:
        notify_execution(session, run=run, rule=rule)
    session.flush()


def run_execution(session: Session, *, tenant_id: str, data: dict[str, Any]) -> str:
    run = session.get(AutomationRuleRun, UUID(data["run_id"]))
    if run is None:
        return "RUN_DELETED"
    if str(run.tenant_id) != tenant_id:
        raise DomainError("action_forbidden", "运行不属于该租户")
    # 先取租户执行锁再重读运行状态：重复投递的第二个任务在锁后看到已执行，直接返回。
    lock_tenant(session, run.tenant_id)
    session.refresh(run)
    if run.execution_status not in ("QUEUED", "CONFIRMED"):
        return run.execution_status
    rule = session.get(AutomationRule, run.rule_id)
    if rule is None:
        return "RULE_DELETED"
    session.refresh(rule)
    if not settings.AUTOMATED_RULES_ENABLED:
        _fail(session, run, "automation_disabled")
        return run.execution_status
    if data["mode"] == "AUTO":
        # 自动执行受规则开关与租户暂停约束；人工确认的批次视为明确授权。
        if not runnable(session, rule):
            run.execution_status = "DISMISSED"
            return "SKIPPED"
        actor_id = rule.updated_by
    else:
        batch = session.exec(
            select(AutomationConfirmation).where(
                AutomationConfirmation.run_id == run.id
            )
        ).one()
        assert batch.decided_by is not None
        actor_id = batch.decided_by
    selected = data.get("selected_keys")
    execute_hits(
        session,
        run=run,
        rule=rule,
        selected_keys=set(selected) if selected is not None else None,
        now=datetime.now(UTC),
        actor_id=actor_id,
        require_manage=data["mode"] == "AUTO",
    )
    notify_execution(session, run=run, rule=rule)
    return run.execution_status
