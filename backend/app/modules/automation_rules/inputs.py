"""判断输入：展开租户账户、加载对象目录、报表事实与新鲜度。只读本地已同步数据。"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.schedule import due_at, next_run_at
from app.modules.automation_rules.schemas import AccountScope, RuleDefinition
from app.modules.reporting.metric_catalog import STORED_KEYS

# 报表热数据 30 分钟、素材 60 分钟同步一次；超过阈值说明同步停滞，宁可跳过也不按旧数据判断。
FRESHNESS = {"ad_day": timedelta(minutes=90), "material_day": timedelta(minutes=150)}
_PAST = {"PAST_3_DAYS": 3, "PAST_5_DAYS": 5, "PAST_7_DAYS": 7}
_LIFETIME_START = date(2000, 1, 1)
_BUDGET_KEYS = ("budget", "daily_budget", "campaign_daily_budget")
_ROAS_KEYS = ("roas_bid", "target_roas")
_PARENT = {"adgroup": "campaign", "ad": "adgroup"}


@dataclass(frozen=True)
class AccountInfo:
    advertiser_id: str
    bc_id: str
    timezone: str
    currency: str
    name: str


@dataclass(frozen=True)
class Entity:
    key: str
    kind: str
    advertiser_id: str
    bc_id: str
    remote_id: str
    name: str
    drama: str | None
    naming_valid: bool
    status: str | None
    currency: str
    timezone: str
    parent_key: str | None
    attributes: dict[str, Decimal | str | None]
    material_uses: tuple[dict[str, Any], ...] = ()
    # 素材所属视频，与素材报表按视频跨账户合并的口径一致；对不上视频时为空。
    video: str | None = None
    # 日报帖子可因重建出现多个；判断按广告内视频合计，操作只用当前身份。
    report_ids: tuple[str, ...] = ()
    judgement_issue: str | None = None


@dataclass
class Inputs:
    accounts: dict[str, AccountInfo]
    entities: dict[str, Entity]
    facts: dict[str, dict[str, dict[str, Decimal | None]]]
    skipped_accounts: list[dict[str, str]] = field(default_factory=list)


def local_today(timezone: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def local_window(range_type: str, timezone: str, now: datetime) -> tuple[date, date]:
    """按账户自身时区取日期；「最近 N 天」含今天。"""
    today = local_today(timezone, now)
    if range_type == "TODAY":
        return today, today
    if range_type == "YESTERDAY":
        return today - timedelta(days=1), today - timedelta(days=1)
    if range_type == "LIFETIME":
        return _LIFETIME_START, today
    return today - timedelta(days=_PAST[range_type] - 1), today


def expand_accounts(
    session: Session, *, tenant_id: UUID, scope: AccountScope
) -> dict[str, AccountInfo]:
    """展开租户内可读账户；同一账户属于多个 BC 时只取一次（最小 BC ID，结果稳定）。"""
    statement = usable_grants(
        tenant_id=tenant_id, bc_id=col(BCAccountAccess.bc_id), action="read"
    )
    if scope.mode == "BC":
        statement = statement.where(col(BCAccountAccess.bc_id).in_(scope.bc_ids))
    elif scope.mode == "ACCOUNTS":
        statement = statement.where(
            col(BCAccountAccess.advertiser_id).in_(scope.advertiser_ids)
        )
    statement = statement.order_by(
        col(BCAccountAccess.advertiser_id), col(BCAccountAccess.bc_id)
    )
    bc_by_account: dict[str, str] = {}
    for grant in session.exec(statement).all():
        bc_by_account.setdefault(grant.advertiser_id, grant.bc_id)
    if not bc_by_account:
        return {}
    rows = session.exec(
        select(AdvertiserAccount).where(
            AdvertiserAccount.tenant_id == tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(list(bc_by_account)),
        )
    ).all()
    return {
        row.advertiser_id: AccountInfo(
            row.advertiser_id,
            bc_by_account[row.advertiser_id],
            row.timezone,
            row.currency,
            row.name,
        )
        for row in sorted(rows, key=lambda r: r.advertiser_id)
    }


def plan_next_run(
    session: Session,
    *,
    tenant_id: UUID,
    definition: RuleDefinition,
    rule_timezone: str,
    after: datetime,
) -> datetime | None:
    """规则的下一次运行时刻。按账户时区时取规则范围内全部账户的时区，最早的槽位先运行；
    暂时没有账户时仍按规则时区排期，账户接入后的下一次排期自动纳入。"""
    zones: set[str] = {rule_timezone}
    if definition.schedule.timezone_basis == "ACCOUNT":
        accounts = expand_accounts(
            session, tenant_id=tenant_id, scope=definition.accounts
        )
        zones = {info.timezone for info in accounts.values()} or zones
    return next_run_at(definition.schedule, after=after, timezones=zones)


def _fresh_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    kind: str,
    now: datetime,
) -> tuple[dict[str, AccountInfo], list[dict[str, str]]]:
    """以账户本地「今天」的同步状态判断新鲜度；本地零点后不久今天尚未同步时看昨天。"""
    if not accounts:
        return {}, []
    rows = session.connection().execute(
        text(
            "SELECT advertiser_id, stat_date, last_success_at FROM report_sync_state "
            "WHERE tenant_id = :t AND report_kind = :k AND advertiser_id = ANY(:advs) "
            "AND stat_date >= :since"
        ),
        {
            "t": tenant_id,
            "k": kind,
            "advs": list(accounts),
            "since": now.date() - timedelta(days=2),
        },
    )
    success = {(r.advertiser_id, r.stat_date): r.last_success_at for r in rows}
    fresh, skipped = {}, []
    for adv, info in accounts.items():
        local_now = now.astimezone(ZoneInfo(info.timezone))
        days = [local_now.date()]
        if local_now - local_now.replace(hour=0, minute=0, second=0) < FRESHNESS[kind]:
            days.append(local_now.date() - timedelta(days=1))
        latest = max(
            (success[(adv, d)] for d in days if success.get((adv, d)) is not None),
            default=None,
        )
        if latest is not None and latest >= now - FRESHNESS[kind]:
            fresh[adv] = info
        else:
            skipped.append({"advertiser_id": adv, "reason": "STALE_REPORT"})
    return fresh, skipped


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    return result if result.is_finite() else None


def _first_number(config: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = _decimal(config.get(key))
        if value:
            return value
    return None


def _created_date(value: Any, timezone: str) -> date | None:
    """目录 create_time：数字为 epoch 秒，字符串按平台 UTC 时间；按账户时区取本地日。"""
    moment: datetime | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("T", " ").removesuffix("Z")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(timezone)).date() if moment else None


def _projections(
    session: Session, tenant_id: UUID, advertisers: list[str]
) -> dict[tuple[str, str], Any]:
    rows = session.connection().execute(
        text(
            "SELECT DISTINCT ON (advertiser_id, campaign_remote_id) advertiser_id, "
            "campaign_remote_id, provider_label, drama_name, status "
            "FROM campaign_name_projection WHERE tenant_id = :t AND advertiser_id = ANY(:advs) "
            "ORDER BY advertiser_id, campaign_remote_id, grouping_revision DESC, "
            "name_revision DESC, observed_at DESC"
        ),
        {"t": tenant_id, "advs": advertisers},
    )
    return {(r.advertiser_id, r.campaign_remote_id): r for r in rows}


def _objects(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    now: datetime,
) -> dict[str, Entity]:
    advertisers = list(accounts)
    rows = (
        session.connection()
        .execute(
            text(
                "SELECT advertiser_id, kind, remote_id, parent_remote_id, name, "
                "operation_status, configuration FROM ad_object "
                "WHERE tenant_id = :t AND advertiser_id = ANY(:advs) "
                "AND kind IN ('campaign','adgroup','ad')"
            ),
            {"t": tenant_id, "advs": advertisers},
        )
        .all()
    )
    projections = _projections(session, tenant_id, advertisers)
    by_key = {(r.advertiser_id, r.kind, r.remote_id): r for r in rows}

    def campaign_of(row: Any) -> str | None:
        # 广告组、广告沿上级链找到所属系列，剧名只来自系列名。
        while row is not None and row.kind != "campaign":
            row = by_key.get(
                (row.advertiser_id, _PARENT[row.kind], row.parent_remote_id)
            )
        return row.remote_id if row is not None else None

    entities: dict[str, Entity] = {}
    for row in rows:
        info = accounts[row.advertiser_id]
        config = row.configuration or {}
        campaign = campaign_of(row)
        projection = (
            projections.get((row.advertiser_id, campaign)) if campaign else None
        )
        valid = projection is not None and projection.status == "VALID"
        created = _created_date(config.get("create_time"), info.timezone)
        days = (
            Decimal((local_today(info.timezone, now) - created).days)
            if created
            else None
        )
        parent_kind = _PARENT.get(row.kind)
        entities[f"{row.kind}:{row.advertiser_id}:{row.remote_id}"] = Entity(
            key=f"{row.kind}:{row.advertiser_id}:{row.remote_id}",
            kind=row.kind,
            advertiser_id=row.advertiser_id,
            bc_id=info.bc_id,
            remote_id=row.remote_id,
            name=row.name or row.remote_id,
            drama=f"{projection.provider_label}-{projection.drama_name}"
            if valid and projection is not None
            else None,
            naming_valid=valid,
            status=row.operation_status,
            currency=info.currency,
            timezone=info.timezone,
            parent_key=f"{parent_kind}:{row.advertiser_id}:{row.parent_remote_id}"
            if parent_kind and row.parent_remote_id
            else None,
            attributes={
                "name": row.name or row.remote_id,
                "budget": _first_number(config, _BUDGET_KEYS),
                "roas_bid": _first_number(config, _ROAS_KEYS),
                "days_since_creation": days,
            },
        )
    return entities


def _materials(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    ads: dict[str, Entity],
    *,
    definition: RuleDefinition | None = None,
    now: datetime | None = None,
) -> dict[str, Entity]:
    # 延迟导入避免 Entity 数据合同与素材加载器循环导入。
    from app.modules.automation_rules.material_inputs import load_material_entities

    return load_material_entities(
        session, tenant_id, accounts, ads, definition=definition, now=now
    )


def _add(
    target: dict[str, Decimal | None], row: Any, *, strict_facts: bool = False
) -> None:
    for key in STORED_KEYS:
        value = getattr(row, key)
        if strict_facts and (value is None or (key in target and target[key] is None)):
            target[key] = None
        elif value is not None:
            target[key] = (target.get(key) or Decimal(0)) + value
        else:
            target.setdefault(key, None)


def _facts(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    range_type: str,
    *,
    material: bool,
    now: datetime,
    strict_facts: bool = False,
) -> dict[str, dict[str, Decimal | None]]:
    windows = [local_window(range_type, accounts[a].timezone, now) for a in accounts]
    # 反向冲突重算要求完整事实：跨天的部分 NULL 不能被 SUM 静默忽略。
    # 普通规则继续沿用已有求和口径，此参数只决定重算证据是否足够。
    sums = ", ".join(
        (
            f"CASE WHEN count(f.{key}) = count(*) THEN sum(f.{key}) END AS {key}"
            if strict_facts
            else f"sum(f.{key}) AS {key}"
        )
        for key in STORED_KEYS
    )
    table, ids = (
        ("material_report_day", "f.main_material_id")
        if material
        else ("ad_report_day", "f.campaign_id, f.adgroup_id, f.ad_id")
    )
    rows = (
        session.connection()
        .execute(
            text(
                f"SELECT f.advertiser_id, {ids}, {sums} FROM {table} f "
                "JOIN unnest(CAST(:advs AS text[]), CAST(:starts AS date[]), CAST(:ends AS date[])) "
                "AS w(adv, s, e) ON w.adv = f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e "
                f"WHERE f.tenant_id = :t GROUP BY f.advertiser_id, {ids}"
            ),
            {
                "t": tenant_id,
                "advs": list(accounts),
                "starts": [w[0] for w in windows],
                "ends": [w[1] for w in windows],
            },
        )
        .all()
    )
    result: dict[str, dict[str, Decimal | None]] = defaultdict(dict)
    for row in rows:
        adv = row.advertiser_id
        if material:
            _add(
                result[f"material:{adv}:{row.main_material_id}"],
                row,
                strict_facts=strict_facts,
            )
            continue
        # 广告日报是唯一明细来源，向上累加到广告组、系列。
        for key in (
            f"ad:{adv}:{row.ad_id}",
            f"adgroup:{adv}:{row.adgroup_id}",
            f"campaign:{adv}:{row.campaign_id}",
        ):
            _add(result[key], row, strict_facts=strict_facts)
    return dict(result)


def load_inputs(
    session: Session,
    *,
    tenant_id: UUID,
    definition: RuleDefinition,
    now: datetime,
    slot_at: datetime | None = None,
    strict_facts: bool = False,
) -> Inputs:
    """slot_at 为定时运行的槽位：按账户时区运行时只判断当地正处于该槽位的账户；
    试运行等不带槽位的判断覆盖全部账户。"""
    accounts = expand_accounts(session, tenant_id=tenant_id, scope=definition.accounts)
    if slot_at is not None and definition.schedule.timezone_basis == "ACCOUNT":
        accounts = {
            adv: info
            for adv, info in accounts.items()
            if due_at(definition.schedule, slot_at=slot_at, timezone=info.timezone)
        }
    level = definition.apply_to.level
    kind = "material_day" if level == "material" else "ad_day"
    accounts, skipped = _fresh_accounts(
        session, tenant_id=tenant_id, accounts=accounts, kind=kind, now=now
    )
    if not accounts:
        return Inputs(accounts={}, entities={}, facts={}, skipped_accounts=skipped)
    entities = _objects(session, tenant_id, accounts, now)
    if "material" in (level, definition.action.level):
        entities.update(
            _materials(
                session,
                tenant_id,
                accounts,
                entities,
                definition=definition if level == "material" else None,
                now=now,
            )
        )
    ranges: list[str] = sorted(
        {
            str(c.range)
            for c in definition.conditions
            if c.range and RULE_METRICS[c.metric].kind == "fact"
        }
    )
    facts: dict[str, dict[str, dict[str, Decimal | None]]] = {}
    for range_type in ranges:
        loaded = _facts(
            session,
            tenant_id,
            accounts,
            range_type,
            material=level == "material",
            strict_facts=strict_facts or level == "material",
            now=now,
        )
        if level == "material":
            from app.modules.automation_rules.material_inputs import material_facts

            facts[range_type] = material_facts(entities, loaded)
        else:
            facts[range_type] = {k: v for k, v in loaded.items() if k in entities}
    if level == "material":
        from app.modules.automation_rules.material_inputs import (
            mark_missing_metrics,
            material_issues,
        )

        mark_missing_metrics(entities, facts, definition)
        skipped.extend(material_issues(entities, definition))
    return Inputs(
        accounts=accounts, entities=entities, facts=facts, skipped_accounts=skipped
    )
