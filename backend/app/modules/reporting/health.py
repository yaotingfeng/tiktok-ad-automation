"""按账户当地当前日监测延迟和覆盖，有界重建缺口；告警与恢复持久可见。"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit
from app.modules.reporting.directory_refresh import (
    GENERAL_DIRECTORY_FRESHNESS,
    rule_accounts,
)
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.planner import (
    _common,
    _group_by_window,
    _hot_window,
    known_reporting_accounts,
    owned_accounts,
    plan_hot,
    plan_material,
)
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealth,
    ReportSyncHealthEvent,
)

logger = logging.getLogger(__name__)
SLO = timedelta(minutes=5)
REPAIR_INTERVAL = timedelta(minutes=2)
MAX_REPAIR_ACCOUNTS = 50


def measure_health(session, plan, *, now, accounts=None):
    owned = owned_accounts(session, plan) if accounts is None else accounts
    params = {
        "t": plan.tenant_id,
        "bc": plan.bc_id,
        "ids": owned,
        "active": sorted(
            set(plan.last_active_advertiser_ids)
            | known_reporting_accounts(session, plan, accounts=owned, now=now)
        ),
        "now": now,
        "cutoff": now - SLO,
        "general_cutoff": now - GENERAL_DIRECTORY_FRESHNESS,
        "strict_accounts": sorted(
            rule_accounts(
                session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=owned
            )
        ),
        "action_cutoff": now - timedelta(minutes=2),
    }
    rows = (
        session.execute(
            text("""
        WITH active AS (
          SELECT a.advertiser_id,(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date AS local_day
          FROM advertiser_account a WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
            AND a.advertiser_id=ANY(:active))
        SELECT a.advertiser_id,kinds.kind AS report_kind,s.status,s.last_success_at
        FROM active a CROSS JOIN (VALUES ('ad_day'),('material_day')) kinds(kind)
        LEFT JOIN report_sync_state s ON s.tenant_id=:t AND s.advertiser_id=a.advertiser_id
          AND s.stat_date=a.local_day AND s.report_kind=kinds.kind
    """),
            params,
        )
        .mappings()
        .all()
    )
    stale = {"ad_day": set(), "material_day": set()}
    times = []
    for row in rows:
        at = row["last_success_at"]
        if at is not None:
            times.append(at)
        if row["status"] != "COMPLETE" or at is None or at < now - SLO:
            stale[row["report_kind"]].add(row["advertiser_id"])
    missing = (
        session.execute(
            text("""
        SELECT r.advertiser_id,r.ad_id FROM ad_report_day r
        LEFT JOIN ad_object a ON a.tenant_id=r.tenant_id AND a.advertiser_id=r.advertiser_id AND a.kind='ad' AND a.remote_id=r.ad_id
        LEFT JOIN ad_object g ON g.tenant_id=r.tenant_id AND g.advertiser_id=r.advertiser_id AND g.kind='adgroup' AND g.remote_id=r.adgroup_id
        LEFT JOIN ad_object c ON c.tenant_id=r.tenant_id AND c.advertiser_id=r.advertiser_id AND c.kind='campaign' AND c.remote_id=r.campaign_id
        WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids) AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND r.spend>0 AND (a.remote_id IS NULL OR g.remote_id IS NULL OR c.remote_id IS NULL
            OR LEAST(a.observed_at,g.observed_at,c.observed_at) < CASE
              WHEN r.advertiser_id=ANY(:strict_accounts) THEN :cutoff ELSE :general_cutoff END)
    """),
            params,
        )
        .mappings()
        .all()
    )
    unlinked = (
        session.execute(
            text("""
        SELECT DISTINCT r.advertiser_id,r.main_material_id FROM material_report_day r
        WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids) AND r.spend>0
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND NOT EXISTS(SELECT 1 FROM material_post_link l WHERE l.tenant_id=r.tenant_id
            AND l.advertiser_id=r.advertiser_id AND l.main_material_id=r.main_material_id
            AND l.platform_material_id IS NOT NULL)
    """),
            params,
        )
        .mappings()
        .all()
    )
    pending = session.execute(
        text("""
        SELECT count(*) FROM management_task WHERE tenant_id=:t AND bc_id=:bc
          AND status='QUEUED' AND created_at<:action_cutoff
    """),
        params,
    ).scalar_one()
    metrics = {
        "stale_ad_accounts": len(stale["ad_day"]),
        "stale_material_accounts": len(stale["material_day"]),
        "missing_directory_ads": len(missing),
        "unlinked_material_posts": len(unlinked),
        "pending_actions": pending,
        "oldest_data_at": min(times).isoformat() if times else None,
    }
    issues = [
        key for key, value in metrics.items() if key != "oldest_data_at" and value
    ]
    repairs = {
        "ad": stale["ad_day"] | {r["advertiser_id"] for r in missing},
        "material": stale["material_day"],
        "links": {r["advertiser_id"] for r in unlinked},
    }
    if owned and (
        plan.active_screened_at is None or plan.active_screened_at < now - SLO
    ):
        # 没有“完整筛选确认为零”的证据，空活跃集合是覆盖未知，不能报健康。
        issues.append("ACCOUNT_SCREEN_STALE")
        unknown = session.execute(
            text("""
            SELECT a.advertiser_id FROM advertiser_account a
            LEFT JOIN report_sync_state s ON s.tenant_id=a.tenant_id AND s.advertiser_id=a.advertiser_id
              AND s.report_kind='ad_day' AND s.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date
            WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
              AND (s.last_success_at IS NULL OR s.last_success_at<:cutoff)
            ORDER BY s.last_success_at ASC NULLS FIRST, a.advertiser_id LIMIT :limit
        """),
            {**params, "limit": MAX_REPAIR_ACCOUNTS},
        ).scalars()
        repairs["ad"].update(unknown)
    return metrics, issues, repairs


def _rescue_queued_actions(session, plan, *, now):
    """只重投原本已授权且仍排队的任务正文；业务执行器继续负责幂等、权限和发送栅栏。"""
    rows = session.execute(
        text("""
        SELECT DISTINCT ON (m.id) p.actor_id,p.task_name,p.payload,m.id
        FROM management_task m JOIN pending_dispatch p ON p.tenant_id=m.tenant_id
          AND p.task_name='ad_management.execute' AND p.payload->>'task_id'=m.id::text
        WHERE m.tenant_id=:t AND m.bc_id=:bc AND m.status='QUEUED' AND m.created_at<:cutoff
        ORDER BY m.id,p.available_at DESC LIMIT 50
    """),
        {"t": plan.tenant_id, "bc": plan.bc_id, "cutoff": now - timedelta(minutes=2)},
    ).mappings()
    count = 0
    for row in rows:
        enqueue_after_commit(
            session,
            context=TenantContext(plan.tenant_id, row["actor_id"], "operator"),
            task_name=row["task_name"],
            task_key=f"sync-action-repair:{row['id']}:{int(now.timestamp()) // 120}",
            payload=row["payload"],
        )
        count += 1
    return count


def observe_health(session, plan, *, now):
    metrics, issues, repairs = measure_health(session, plan, now=now)
    status = "DEGRADED" if issues else "HEALTHY"
    row = session.get(ReportSyncHealth, (plan.tenant_id, plan.bc_id))
    if row is None:
        row = ReportSyncHealth(tenant_id=plan.tenant_id, bc_id=plan.bc_id)
        session.add(row)
    changed = row.status != status or row.issues != issues
    if changed:
        session.add(
            ReportSyncHealthEvent(
                tenant_id=plan.tenant_id,
                bc_id=plan.bc_id,
                status=status,
                metrics=metrics,
                issues=issues,
                created_at=now,
            )
        )
        logger.warning(
            "sync health changed tenant=%s bc=%s status=%s issues=%s",
            plan.tenant_id,
            plan.bc_id,
            status,
            issues,
        )
    row.status, row.metrics, row.issues, row.observed_at = status, metrics, issues, now
    if issues and (
        row.last_repair_at is None or row.last_repair_at <= now - REPAIR_INTERVAL
    ):
        count = plan_hot(
            session, plan, active=sorted(repairs["ad"])[:MAX_REPAIR_ACCOUNTS], now=now
        )
        count += plan_material(
            session,
            plan,
            active=sorted(repairs["material"])[:MAX_REPAIR_ACCOUNTS],
            now=now,
        )
        for (start, end), ids in _group_by_window(
            session,
            plan,
            sorted(repairs["links"])[:MAX_REPAIR_ACCOUNTS],
            lambda tz: _hot_window(tz, now),
        ).items():
            for advertiser in ids:
                count += bool(
                    enqueue_job(
                        session,
                        kind="material_links",
                        tier="material",
                        advertiser_ids=[advertiser],
                        start=start,
                        end=end,
                        **_common(plan),
                    )
                )
        count += _rescue_queued_actions(session, plan, now=now)
        if count:
            row.last_repair_at = now
        # 调度唤醒与恢复计划同事务，不发外部广告操作、不篡改上次成功时间。
        enqueue_after_commit(
            session,
            context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
            task_name="reporting.plan_due",
            task_key=f"sync-health:{plan.bc_id}:{now.replace(second=0, microsecond=0).isoformat()}",
            payload={"dispatch_only": True},
        )
    session.flush()
    return row


@celery_app.task(name="reporting.check_health", time_limit=25, soft_time_limit=20)
def check_health():
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=10)
    count = 0
    with Session(engine) as session:
        keys = session.exec(
            select(AdSyncBc.tenant_id, AdSyncBc.bc_id)
            .outerjoin(
                ReportSyncHealth,
                (col(ReportSyncHealth.tenant_id) == col(AdSyncBc.tenant_id))
                & (col(ReportSyncHealth.bc_id) == col(AdSyncBc.bc_id)),
            )
            .where(col(AdSyncBc.enabled).is_(True))
            .order_by(
                col(ReportSyncHealth.observed_at).asc().nulls_first(),
                col(AdSyncBc.tenant_id),
                col(AdSyncBc.bc_id),
            )
        ).all()
    for key in keys:
        if datetime.now(UTC) >= deadline:
            break
        try:
            with Session(engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if plan is not None and plan.enabled:
                    observe_health(session, plan, now=now)
                    count += 1
        except Exception:
            logger.exception("sync health check failed tenant=%s bc=%s", *key)
            # 单个BC慢查询不会饿死后面的监控；失败本身持久可见，下一轮先检查更旧BC。
            with Session(engine) as session, session.begin():
                row = session.get(ReportSyncHealth, key)
                if row is None:
                    row = ReportSyncHealth(tenant_id=key[0], bc_id=key[1])
                    session.add(row)
                row.status, row.issues, row.observed_at = (
                    "DEGRADED",
                    ["MONITOR_QUERY_FAILED"],
                    now,
                )
    return count
