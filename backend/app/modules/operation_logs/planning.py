"""每日近期回刷与历史游标独立推进，复用账户归属和持久需求公平调度。"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.modules.operation_logs.models import OperationCoverage, OperationSyncCursor
from app.modules.reporting.jobs import enqueue_jobs
from app.modules.reporting.planner import (
    _common,
    _timezones,
    local_today,
    owned_accounts,
)
from app.modules.reporting.store_models import AdSyncBc
from app.modules.sync_control.models import SyncRequirement, SyncScope


def plan_logs(
    session: Session,
    plan: AdSyncBc,
    *,
    now: datetime,
    accounts: list[str] | None = None,
    history_start: date | None = None,
) -> int:
    owned = set(owned_accounts(session, plan))
    candidates = (
        sorted(owned.intersection(accounts)) if accounts is not None else sorted(owned)
    )
    if not candidates:
        return 0
    zones = _timezones(session, plan.tenant_id, candidates)
    session.execute(
        insert(OperationSyncCursor)
        .values(
            [
                OperationSyncCursor(
                    tenant_id=plan.tenant_id, advertiser_id=a
                ).model_dump()
                for a in candidates
            ]
        )
        .on_conflict_do_nothing()
    )
    cursors = session.exec(
        select(OperationSyncCursor)
        .where(
            OperationSyncCursor.tenant_id == plan.tenant_id,
            col(OperationSyncCursor.advertiser_id).in_(candidates),
        )
        .order_by(col(OperationSyncCursor.advertiser_id))
        .with_for_update()
    ).all()
    windows: dict[tuple[str, date, date], list[list[str]]] = defaultdict(list)
    for cursor in cursors:
        advertiser = cursor.advertiser_id
        today = local_today(zones[advertiser], now)
        end = today - timedelta(days=1)
        if cursor.recent_day != today:
            windows["daily", end - timedelta(days=6), end].append([advertiser])
            cursor.recent_day = today
        if history_start is not None and history_start < end - timedelta(days=6):
            if cursor.history_cursor is not None:
                earliest = session.exec(
                    select(func.min(col(SyncRequirement.bucket_key)))
                    .join(SyncScope, col(SyncScope.id) == col(SyncRequirement.scope_id))
                    .where(
                        SyncScope.tenant_id == plan.tenant_id,
                        SyncScope.advertiser_id == advertiser,
                        SyncScope.resource == "operation_log",
                    )
                ).one()
                if earliest is not None and history_start.isoformat() < earliest:
                    # 用户向更早日期扩展经营范围，不等待次日游标轮转。
                    cursor.history_cursor, cursor.history_sweep_day = (
                        history_start,
                        None,
                    )
            start = max(history_start, cursor.history_cursor or history_start)
            history_end = end - timedelta(days=7)
            if start > history_end and cursor.history_sweep_day != today:
                start = history_start
                cursor.history_sweep_day = today
            if start <= history_end:
                # 每轮一片历史；扫描完后最多每天重试一轮失败缺口，避免无限重建。
                last = min(history_end, start + timedelta(days=28))
                known = set(
                    session.exec(
                        select(col(OperationCoverage.stat_date)).where(
                            OperationCoverage.tenant_id == plan.tenant_id,
                            OperationCoverage.advertiser_id == advertiser,
                            OperationCoverage.stat_date >= start,
                            OperationCoverage.stat_date <= last,
                        )
                    ).all()
                )
                if len(known) < (last - start).days + 1:
                    windows["history", start, last].append([advertiser])
                cursor.history_cursor = last + timedelta(days=1)
        session.add(cursor)
    created = 0
    for (tier, start, end), groups in windows.items():
        created += len(
            enqueue_jobs(
                session,
                **_common(plan),
                kind="operation_log",
                tier=tier,
                advertiser_groups=groups,
                start=start,
                end=end,
                requested_at=now,
                execution_window_days=29,
            )
        )
    return created
