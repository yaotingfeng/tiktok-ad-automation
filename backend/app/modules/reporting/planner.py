"""报表同步计划：每个已绑定官方 API 连接的 BC 一行，授权完成即自动建立。

一个账户若被多个 BC 授权，只由 bc_id 字典序最小的 BC 计划拉取，避免重复调用；
数据按账户存放，各 BC 的可见性由查询层按当前授权判断。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, text
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import ReportReader
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import AdvertiserAccount, DiscoveryRun
from app.modules.accounts.routing import freeze_route
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.partitions import ensure_month_partitions_for_range
from app.modules.reporting.store_models import (
    AdSyncAccount,
    AdSyncBc,
    ReportSyncState,
)

HOT_INTERVAL = timedelta(minutes=30)
MATERIAL_INTERVAL = timedelta(minutes=60)
DIRECTORY_INTERVAL = timedelta(hours=3)
REFRESH_DAYS = 32
DAILY_AT = time(12, 30)  # 平台 UTC 12:00 修正数据之后
ROWS_PER_BATCH_TARGET = 15_000  # 预估行数超过即减少合并账户（平台上限 2 万）


def local_today(timezone: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def _next_daily(now: datetime) -> datetime:
    target = datetime.combine(now.date(), DAILY_AT, UTC)
    return target if target > now else target + timedelta(days=1)


def _actor(session, tenant_id: UUID, bc_id: str, connection_id: UUID) -> UUID | None:
    """后台身份：该 BC 最近一次完成账户发现的操作者。"""
    run = session.exec(
        select(DiscoveryRun)
        .where(
            DiscoveryRun.tenant_id == tenant_id,
            DiscoveryRun.connection_id == connection_id,
            # Official API account discovery is connection-wide, so its
            # completed run intentionally has no BC scope.  Keep accepting
            # exact-BC runs for older/MCP-shaped records, but never cross the
            # tenant or connection boundary above.
            or_(col(DiscoveryRun.bc_id) == bc_id, col(DiscoveryRun.bc_id).is_(None)),
            DiscoveryRun.status == "COMPLETE",
        )
        .order_by(
            col(DiscoveryRun.completed_at).desc().nulls_last(),
            col(DiscoveryRun.created_at).desc(),
        )
    ).first()
    return run.actor_id if run else None


def ensure_bc_plans(
    session: Session, *, now: datetime, tenant_id: UUID | None = None
) -> int:
    enabled = 0
    scope_tenant = tenant_id
    binding_query = select(BCConnectionBinding).where(
        BCConnectionBinding.status == "ACTIVE"
    )
    plan_query = select(AdSyncBc)
    if scope_tenant is not None:
        binding_query = binding_query.where(
            BCConnectionBinding.tenant_id == scope_tenant
        )
        plan_query = plan_query.where(AdSyncBc.tenant_id == scope_tenant)
    bindings = session.exec(binding_query).all()
    # 同 BC 有多个 API 绑定时稳定选择一个，不能受数据库返回顺序影响。
    api = {
        (b.tenant_id, b.bc_id): b
        for b in sorted(bindings, key=lambda b: str(b.connection_id), reverse=True)
        if b.kind == "OFFICIAL_API"
    }
    existing = session.exec(plan_query).all()
    for tenant_id, bc_id in {(b.tenant_id, b.bc_id) for b in bindings} | {
        (p.tenant_id, p.bc_id) for p in existing
    }:
        plan = session.get(AdSyncBc, (tenant_id, bc_id))
        binding = api.get((tenant_id, bc_id))
        if binding is None:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, "api_binding_required"
            continue
        actor = _actor(session, tenant_id, bc_id, binding.connection_id) or (
            plan.actor_id if plan else None
        )
        if actor is None:
            continue  # 尚无完成的账户发现：等待授权流程完成
        context = TenantContext(tenant_id=tenant_id, actor_id=actor, role="operator")
        try:
            route = freeze_route(
                session,
                context=context,
                bc_id=bc_id,
                connection_id=binding.connection_id,
            )
        except DomainError as error:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, error.code
            continue
        route_json = route.model_dump(mode="json")
        if plan is None:
            plan = AdSyncBc(
                tenant_id=tenant_id,
                bc_id=bc_id,
                frozen_route=route_json,
                actor_id=actor,
                next_hot_at=now,
                next_material_at=now,
                next_directory_at=now,
                # 新授权的 BC 立即回刷近 32 天；历史回补只覆盖 32 天之前，
                # 若等到下一个每日时点，中间这段数据会一直为空。
                next_daily_at=now,
            )
            session.add(plan)
        plan.frozen_route, plan.actor_id, plan.enabled, plan.disabled_reason = (
            route_json,
            actor,
            True,
            None,
        )
        plan.updated_at = now
        enabled += 1
    session.flush()
    _refresh_owners(session, tenant_id=scope_tenant)
    return enabled


def _refresh_owners(session, *, tenant_id=None) -> None:
    session.connection().execute(
        text("""
        INSERT INTO ad_sync_account (tenant_id, advertiser_id, owner_bc_id)
        SELECT a.tenant_id, a.advertiser_id, MIN(a.bc_id)
        FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
          AND a.connection_id::text = p.frozen_route->>'connection_id'
        WHERE (CAST(:tenant AS uuid) IS NULL OR a.tenant_id=CAST(:tenant AS uuid)) AND a.in_bc AND a.authorized AND a.active
        GROUP BY a.tenant_id, a.advertiser_id
        ON CONFLICT (tenant_id, advertiser_id) DO UPDATE SET owner_bc_id = EXCLUDED.owner_bc_id"""),
        {"tenant": tenant_id},
    )
    session.connection().execute(
        text("""
        DELETE FROM ad_sync_account s WHERE (CAST(:tenant AS uuid) IS NULL OR s.tenant_id=CAST(:tenant AS uuid)) AND NOT EXISTS (
          SELECT 1 FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
          AND a.connection_id::text = p.frozen_route->>'connection_id'
          WHERE a.tenant_id=s.tenant_id AND a.advertiser_id=s.advertiser_id AND a.in_bc AND a.authorized AND a.active)"""),
        {"tenant": tenant_id},
    )


def account_owners(session, *, tenant_id: UUID) -> dict[str, str]:
    return dict(
        session.exec(
            select(AdSyncAccount.advertiser_id, AdSyncAccount.owner_bc_id).where(
                AdSyncAccount.tenant_id == tenant_id
            )
        ).all()
    )


def owned_accounts(session, plan: AdSyncBc) -> list[str]:
    return sorted(
        session.exec(
            select(AdSyncAccount.advertiser_id).where(
                AdSyncAccount.tenant_id == plan.tenant_id,
                AdSyncAccount.owner_bc_id == plan.bc_id,
            )
        ).all()
    )


def due_plans(session, *, now: datetime, limit: int = 50) -> list[AdSyncBc]:
    return list(
        session.exec(
            select(AdSyncBc)
            .where(
                col(AdSyncBc.enabled).is_(True),
                exists().where(
                    col(AdSyncAccount.tenant_id) == col(AdSyncBc.tenant_id),
                    col(AdSyncAccount.owner_bc_id) == col(AdSyncBc.bc_id),
                ),
                (AdSyncBc.next_hot_at <= now)
                | (AdSyncBc.next_material_at <= now)
                | (AdSyncBc.next_daily_at <= now)
                | (AdSyncBc.next_directory_at <= now),
            )
            .order_by(col(AdSyncBc.next_hot_at))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )


def screen_active(
    reader: ReportReader, *, bc_id: str, start: date, end: date, candidates: set[str]
) -> set[str]:
    """BC 报表一次返回整个 BC 下账户的消耗；只保留本计划负责的账户。"""
    active, page = set(), 1
    while True:
        result = reader.bc_spend(bc_id=bc_id, start=start, end=end, page=page)
        for row in result.rows:
            advertiser = str((row.get("dimensions") or {}).get("advertiser_id"))
            spend = (row.get("metrics") or {}).get("spend")
            if (
                advertiser in candidates
                and spend not in (None, "-")
                and float(spend) > 0
            ):
                active.add(advertiser)
        if result.next_page is None:
            return active
        page = result.next_page


def batch_accounts(
    session, *, tenant_id, advertiser_ids: Sequence[str], days: int
) -> list[list[str]]:
    """按近 7 天单日最大广告行数估计每个账户的行量，合并到不超过 5 个且预估 ≤ 1.5 万行。"""
    sizes = dict(
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id, MAX(n) FROM (
          SELECT advertiser_id, stat_date, COUNT(*) n FROM ad_report_day
          WHERE tenant_id=:t AND advertiser_id = ANY(:a) AND stat_date >= CURRENT_DATE - 7
          GROUP BY advertiser_id, stat_date) x GROUP BY advertiser_id"""),
            {"t": tenant_id, "a": list(advertiser_ids)},
        )
        .all()
    )
    batches, current, load = [], [], 0
    for advertiser in sorted(advertiser_ids):
        estimate = sizes.get(advertiser, 100) * days
        if current and (len(current) == 5 or load + estimate > ROWS_PER_BATCH_TARGET):
            batches.append(current)
            current, load = [], 0
        current.append(advertiser)
        load += estimate
    if current:
        batches.append(current)
    return batches


def _timezones(session, tenant_id, advertiser_ids) -> dict[str, str]:
    return dict(
        session.exec(
            select(AdvertiserAccount.advertiser_id, AdvertiserAccount.timezone).where(
                AdvertiserAccount.tenant_id == tenant_id,
                col(AdvertiserAccount.advertiser_id).in_(advertiser_ids),
            )
        ).all()
    )


def _hot_window(timezone: str, now: datetime) -> tuple[date, date]:
    today = local_today(timezone, now)
    # UTC 12:00 前平台还会修正昨天的数据，热刷一并覆盖昨天。
    start = (
        today - timedelta(days=1) if now.astimezone(UTC).time() < time(12, 0) else today
    )
    return start, today


def _common(plan: AdSyncBc) -> dict:
    return {
        "tenant_id": plan.tenant_id,
        "bc_id": plan.bc_id,
        "frozen_route": plan.frozen_route,
        "actor_id": plan.actor_id,
    }


def _group_by_window(
    session, plan, advertisers, window_fn
) -> dict[tuple[date, date], list[str]]:
    grouped: dict[tuple[date, date], list[str]] = {}
    for advertiser, timezone in _timezones(
        session, plan.tenant_id, advertisers
    ).items():
        grouped.setdefault(window_fn(timezone), []).append(advertiser)
    return grouped


# 筛选时平台确认窗口内零消耗的账户不会再建报表任务；为它们记一条“已完成、
# 0 行”的同步状态，否则这些账户永远显示缺数据，汇总也会因一个缺失成员整体为空。
CONFIRMED_EMPTY_KINDS = ("ad_day", "campaign_day_utc", "account_day", "material_day")


def mark_confirmed_empty(
    session, plan: AdSyncBc, *, inactive: Sequence[str], window_fn, now: datetime
) -> int:
    """平台筛查确认这些账户在窗口内无消耗：补齐从未成功或失败的日期为完成 0 行。

    已完成的日期保留原有事实与行数，只把确认时间推进到本次筛查时间。否则全部账户
    无消耗时没有任何报表任务会更新时间，页面"数据更新到"会一直停在最后一次有消耗
    的同步时刻，看起来像同步停了。
    """
    marked = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, inactive, window_fn
    ).items():
        day = start
        while day <= end:
            for advertiser in advertisers:
                for kind in CONFIRMED_EMPTY_KINDS:
                    key = (plan.tenant_id, advertiser, kind, day)
                    state = session.get(ReportSyncState, key)
                    if state is not None and state.status == "COMPLETE":
                        state.last_success_at = state.updated_at = now
                        continue
                    if state is None:
                        state = ReportSyncState(
                            tenant_id=plan.tenant_id,
                            advertiser_id=advertiser,
                            report_kind=kind,
                            stat_date=day,
                            status="COMPLETE",
                        )
                        session.add(state)
                    state.status, state.error_code, state.row_count = (
                        "COMPLETE",
                        None,
                        0,
                    )
                    state.last_success_at = state.updated_at = now
                    marked += 1
            day += timedelta(days=1)
    session.flush()
    return marked


def plan_hot(
    session, plan: AdSyncBc, *, active: Sequence[str], now: datetime, tier: str = "hot"
) -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, active, lambda tz: _hot_window(tz, now)
    ).items():
        for batch in batch_accounts(
            session,
            tenant_id=plan.tenant_id,
            advertiser_ids=advertisers,
            days=(end - start).days + 1,
        ):
            count += bool(
                enqueue_job(
                    session,
                    kind="ad_day",
                    tier=tier,
                    advertiser_ids=batch,
                    start=start,
                    end=end,
                    **_common(plan),
                )
            )
            # 小时报表始终覆盖本地今天和昨天，不受广告日 UTC 中午窗口影响。
            day = end - timedelta(days=1)
            while day <= end:
                count += bool(
                    enqueue_job(
                        session,
                        kind="campaign_hour",
                        tier=tier,
                        advertiser_ids=batch,
                        start=day,
                        end=day,
                        **_common(plan),
                    )
                )
                day += timedelta(days=1)
    return count


def plan_material(session, plan, *, active, now, tier: str = "material") -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, active, lambda tz: _hot_window(tz, now)
    ).items():
        for advertiser in advertisers:  # 素材报表不支持多账户合并
            count += bool(
                enqueue_job(
                    session,
                    kind="material_day",
                    tier=tier,
                    advertiser_ids=[advertiser],
                    start=start,
                    end=end,
                    **_common(plan),
                )
            )
    return count


def _shards(start: date, end: date, days: int = 30):
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def daily_window(now: datetime):
    """每日回刷窗口：各账户本地今天往前共 REFRESH_DAYS 天。"""

    def window(tz):
        return (
            local_today(tz, now) - timedelta(days=REFRESH_DAYS - 1),
            local_today(tz, now),
        )

    return window


def plan_daily(session, plan, *, active_32d, now) -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(
        session, plan, active_32d, daily_window(now)
    ).items():
        for s, e in _shards(start, end):
            for batch in batch_accounts(
                session,
                tenant_id=plan.tenant_id,
                advertiser_ids=advertisers,
                days=(e - s).days + 1,
            ):
                for kind in ("ad_day", "campaign_day_utc", "account_day"):
                    count += bool(
                        enqueue_job(
                            session,
                            kind=kind,
                            tier="daily",
                            advertiser_ids=batch,
                            start=s,
                            end=e,
                            **_common(plan),
                        )
                    )
            for advertiser in advertisers:
                count += bool(
                    enqueue_job(
                        session,
                        kind="material_day",
                        tier="daily",
                        advertiser_ids=[advertiser],
                        start=s,
                        end=e,
                        **_common(plan),
                    )
                )
    return count


def plan_history(session, plan, *, now) -> int:
    """授权后一次：开户日至回刷窗口之前，按 30 天切片；已规划的账户不再重复。"""
    count = 0
    rows = session.exec(
        select(AdSyncAccount, AdvertiserAccount)
        .join(
            AdvertiserAccount,
            (col(AdvertiserAccount.tenant_id) == AdSyncAccount.tenant_id)
            & (col(AdvertiserAccount.advertiser_id) == AdSyncAccount.advertiser_id),
        )
        .where(
            AdSyncAccount.tenant_id == plan.tenant_id,
            AdSyncAccount.owner_bc_id == plan.bc_id,
            col(AdSyncAccount.history_planned_at).is_(None),
        )
    ).all()
    for owner, account in rows:
        if account.remote_created_at is None or not account.timezone:
            continue  # 开户时间未知：等账户目录补齐后再规划
        end = local_today(account.timezone, now) - timedelta(days=REFRESH_DAYS)
        start = account.remote_created_at.astimezone(ZoneInfo(account.timezone)).date()
        if start <= end:
            ensure_month_partitions_for_range(session, start, end)
            for s, e in _shards(start, end):
                for kind in ("ad_day", "campaign_day_utc"):
                    count += bool(
                        enqueue_job(
                            session,
                            kind=kind,
                            tier="history",
                            advertiser_ids=[account.advertiser_id],
                            start=s,
                            end=e,
                            **_common(plan),
                        )
                    )
                count += bool(
                    enqueue_job(
                        session,
                        kind="material_day",
                        tier="history",
                        advertiser_ids=[account.advertiser_id],
                        start=s,
                        end=e,
                        **_common(plan),
                    )
                )
        owner.history_planned_at = now
    session.flush()
    return count


def estimate_round_requests(
    *, bcs: int, active: int, batch: int, hour_pages: float, ad_pages: float
) -> int:
    """一轮热刷请求量估算（素材按小时一次，折半计入每 30 分钟一轮）。"""
    batches = ceil(active / batch)
    return int(bcs + batches * ad_pages + batches * hour_pages + active / 2)


def _calls_key(now: datetime) -> str:
    return f"tiktok:report:calls:{now:%Y%m%d}"


def record_calls(redis, now: datetime, count: int) -> None:
    key = _calls_key(now)
    redis.incrby(key, count)
    redis.expire(key, 2 * 86400)


def calls_today(redis, now: datetime) -> int:
    return int(redis.get(_calls_key(now)) or 0)
