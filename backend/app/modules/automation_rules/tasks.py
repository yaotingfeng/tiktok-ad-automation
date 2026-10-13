"""自动规则后台任务：Beat 扫描到期规则，经 outbox 投递运行与飞书发送。"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from app.modules.automation_rules.runner import deliver_notification, execute_run
from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.automation_rules.settings_service import load_tenant_setting

register_dispatch_task("automation.run_rule", "ads-reporting")

SCAN_LIMIT = 200
RUN_RETENTION = timedelta(days=30)


def scan_due_rules(session: Session, *, now: datetime) -> int:
    """投递到期规则并推进下一次运行时刻；部署开关关闭时不调度任何规则。"""
    if not settings.AUTOMATED_RULES_ENABLED:
        return 0
    rules = session.exec(
        select(AutomationRule)
        .where(
            col(AutomationRule.enabled).is_(True),
            col(AutomationRule.next_run_at) <= now,
        )
        .order_by(col(AutomationRule.next_run_at))
        .limit(SCAN_LIMIT)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for rule in rules:
        setting = load_tenant_setting(session, rule.tenant_id)
        if setting.paused:
            # 租户暂停全部规则：保持到期状态，恢复后立即补跑一次而不是逐个补齐错过的槽位。
            continue
        slot = rule.next_run_at
        assert slot is not None
        enqueue_after_commit(
            session,
            context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
            task_name="automation.run_rule",
            task_key=f"automation-run:{rule.id}:{slot.isoformat()}",
            payload={"rule_id": str(rule.id), "slot_at": slot.isoformat()},
        )
        definition = RuleDefinition.model_validate(rule.definition)
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


def _payload(payload: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    payload = validate_dispatch_payload(payload)
    if set(payload) != keys:
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
