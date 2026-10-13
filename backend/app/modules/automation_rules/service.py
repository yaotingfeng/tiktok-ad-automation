"""规则增删改查、指标目录与试运行。权限每次按动作从数据库重建。"""

from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.automation_rules.catalog import ACTION_LEVELS, RULE_METRICS
from app.modules.automation_rules.evaluate import Hit, evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import (
    CatalogMetric,
    CatalogPublic,
    DryRunPublic,
    HitPublic,
    RuleCreate,
    RuleDefinition,
    RulePage,
    RulePublic,
    RuleUpdate,
    RunSummary,
)
from app.modules.automation_rules.settings_service import load_tenant_setting
from app.modules.reporting.metric_catalog import metric_name_zh, table_label
from app.modules.tenants.permissions import require_tenant

ATTRIBUTE_NAMES = {
    "name": "名称",
    "days_since_creation": "创建天数",
    "budget": "当前预算",
    "roas_bid": "当前 ROAS 出价",
}
DRY_RUN_HIT_LIMIT = 500
DRY_RUN_TARGET_LIMIT = 50


def require(session: Session, context: TenantContext, action: str) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def _load(session: Session, context: TenantContext, rule_id: UUID) -> AutomationRule:
    rule = session.get(AutomationRule, rule_id)
    if rule is None or rule.tenant_id != context.tenant_id:
        raise DomainError("automation_rule_not_found", "规则不存在")
    return rule


def _check_mode(definition: RuleDefinition) -> None:
    # 阶段 1 只开放预警；自动执行与确认后执行在执行通道上线后开放。
    if definition.mode != "NOTIFY":
        raise DomainError("automation_mode_unavailable", "当前版本只支持「仅通知」模式")


def _reschedule(session: Session, rule: AutomationRule, now: datetime) -> None:
    if not rule.enabled:
        rule.next_run_at = None
        return
    definition = RuleDefinition.model_validate(rule.definition)
    setting = load_tenant_setting(session, rule.tenant_id)
    rule.next_run_at = next_run_at(
        definition.schedule, after=now, timezone=setting.schedule_timezone
    )


def run_summary(run: AutomationRuleRun) -> RunSummary:
    return RunSummary.model_validate(run, from_attributes=True)


def _public(rule: AutomationRule, last_run: AutomationRuleRun | None) -> RulePublic:
    return RulePublic(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        version=rule.version,
        definition=RuleDefinition.model_validate(rule.definition),
        created_by=rule.created_by,
        updated_by=rule.updated_by,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        next_run_at=rule.next_run_at,
        last_run_at=rule.last_run_at,
        last_run=run_summary(last_run) if last_run else None,
    )


def _latest_runs(
    session: Session, rule_ids: list[UUID]
) -> dict[UUID, AutomationRuleRun]:
    if not rule_ids:
        return {}
    ids = (
        session.connection()
        .execute(
            text(
                "SELECT DISTINCT ON (rule_id) id FROM automation_rule_run "
                "WHERE rule_id = ANY(:ids) ORDER BY rule_id, started_at DESC"
            ),
            {"ids": rule_ids},
        )
        .scalars()
        .all()
    )
    runs = session.exec(
        select(AutomationRuleRun).where(col(AutomationRuleRun.id).in_(ids))
    ).all()
    return {run.rule_id: run for run in runs}


def _save(session: Session, rule: AutomationRule) -> None:
    """名称在租户内唯一；冲突时回滚到保存点并返回业务错误。"""
    try:
        with session.begin_nested():
            session.add(rule)
            session.flush()
    except IntegrityError as exc:
        if "uq_automation_rule_name" in str(exc.orig):
            raise DomainError(
                "automation_rule_name_conflict", "规则名称已存在"
            ) from None
        raise


def list_rules(session: Session, *, context: TenantContext) -> RulePage:
    require(session, context, "read")
    rules = session.exec(
        select(AutomationRule)
        .where(AutomationRule.tenant_id == context.tenant_id)
        .order_by(col(AutomationRule.updated_at).desc())
    ).all()
    latest = _latest_runs(session, [rule.id for rule in rules])
    return RulePage(
        items=[_public(rule, latest.get(rule.id)) for rule in rules], total=len(rules)
    )


def get_rule(session: Session, *, context: TenantContext, rule_id: UUID) -> RulePublic:
    require(session, context, "read")
    rule = _load(session, context, rule_id)
    return _public(rule, _latest_runs(session, [rule.id]).get(rule.id))


def create_rule(
    session: Session,
    *,
    context: TenantContext,
    body: RuleCreate,
    now: datetime | None = None,
) -> RulePublic:
    require(session, context, "ads_manage")
    _check_mode(body.definition)
    now = now or datetime.now(UTC)
    rule = AutomationRule(
        tenant_id=context.tenant_id,
        name=body.name.strip(),
        enabled=body.enabled,
        definition=body.definition.model_dump(mode="json"),
        created_by=context.actor_id,
        updated_by=context.actor_id,
        created_at=now,
        updated_at=now,
    )
    _reschedule(session, rule, now)
    _save(session, rule)
    return _public(rule, None)


def update_rule(
    session: Session,
    *,
    context: TenantContext,
    rule_id: UUID,
    body: RuleUpdate,
    now: datetime | None = None,
) -> RulePublic:
    require(session, context, "ads_manage")
    _check_mode(body.definition)
    rule = _load(session, context, rule_id)
    if body.expected_version != rule.version:
        raise DomainError(
            "automation_rule_version_conflict", "规则已被他人修改，请刷新后重试"
        )
    now = now or datetime.now(UTC)
    rule.name = body.name.strip()
    rule.enabled = body.enabled
    rule.definition = body.definition.model_dump(mode="json")
    rule.version += 1
    rule.updated_by, rule.updated_at = context.actor_id, now
    _reschedule(session, rule, now)
    _save(session, rule)
    return get_rule(session, context=context, rule_id=rule.id)


def set_enabled(
    session: Session, *, context: TenantContext, rule_id: UUID, enabled: bool
) -> RulePublic:
    require(session, context, "ads_manage")
    rule = _load(session, context, rule_id)
    rule.enabled = enabled
    rule.updated_by, rule.updated_at = context.actor_id, datetime.now(UTC)
    _reschedule(session, rule, rule.updated_at)
    session.flush()
    return get_rule(session, context=context, rule_id=rule.id)


def delete_rule(session: Session, *, context: TenantContext, rule_id: UUID) -> None:
    require(session, context, "ads_manage")
    session.delete(_load(session, context, rule_id))
    session.flush()


def hit_public(hit: Hit, *, target_limit: int | None = None) -> HitPublic:
    targets = [asdict(target) for target in hit.targets]
    return HitPublic(
        subject_key=hit.subject_key,
        subject_label=hit.subject_label,
        drama_name=hit.drama,
        advertiser_id=hit.advertiser_id,
        bc_id=hit.bc_id,
        currency=hit.currency,
        metrics=hit.metrics,
        target_count=len(targets),
        targets=targets[:target_limit] if target_limit else targets,
    )


def dry_run(
    session: Session,
    *,
    context: TenantContext,
    definition: RuleDefinition,
    now: datetime | None = None,
) -> DryRunPublic:
    """按当前数据判断一次；只返回结果，不写运行记录、不发通知。"""
    require(session, context, "read")
    inputs = load_inputs(
        session,
        tenant_id=context.tenant_id,
        definition=definition,
        now=now or datetime.now(UTC),
    )
    result = evaluate(definition, inputs)
    return DryRunPublic(
        checked_count=result.checked,
        hit_count=len(result.hits),
        skipped_count=result.skipped_count,
        skipped_accounts=inputs.skipped_accounts,
        hits=[
            hit_public(hit, target_limit=DRY_RUN_TARGET_LIMIT)
            for hit in result.hits[:DRY_RUN_HIT_LIMIT]
        ],
    )


def metric_label(key: str) -> str:
    if key in ATTRIBUTE_NAMES:
        return ATTRIBUTE_NAMES[key]
    return table_label(metric_name_zh(key, "basic"))


def catalog() -> CatalogPublic:
    return CatalogPublic(
        metrics=[
            CatalogMetric(
                key=metric.key,
                group=metric.group,
                name_zh=metric_label(metric.key),
                unit=metric.unit,
                kind=metric.kind,
                levels=[
                    level
                    for level in ("campaign", "adgroup", "ad", "material")
                    if level in metric.levels
                ],
                aggregations=[
                    a for a in ("EACH", "DRAMA", "ALL") if a in metric.aggregations
                ],
                operators=list(metric.operators),
                ranged=metric.ranged,
            )
            for metric in RULE_METRICS.values()
        ],
        action_levels={key: list(levels) for key, levels in ACTION_LEVELS.items()},
    )
