"""按账户当地当前日监测延迟和覆盖，有界重建缺口；告警与恢复持久可见。"""

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any, TypedDict
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.outbox import enqueue_after_commit, validate_dispatch_payload
from app.jobs.tasks import register_dispatch_task
from app.modules.reporting.directory_refresh import (
    PARENT_INTERVAL,
    STATUS_INTERVAL,
    directory_interval,
    rule_accounts,
)
from app.modules.reporting.freshness import FRESHNESS, fresh_report_success
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.planner import (
    REFRESH_MARGIN,
    _common,
    _group_by_window,
    _hot_window,
    known_reporting_accounts,
    owned_accounts,
    plan_hot,
    plan_material,
)
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import (
    AdSyncBc,
    ReportSyncHealth,
    ReportSyncHealthEvent,
)
from app.modules.tenants.permissions import require_tenant

logger = logging.getLogger(__name__)
# 一个实时轮次外加执行余量，避免正常等待下一轮被健康修复反复补拉。
SLO = FRESHNESS["ad_day"]
REPAIR_INTERVAL = timedelta(minutes=2)
MAX_REPAIR_ACCOUNTS = 50
REPAIR_BUDGET_SECONDS = 2
HEALTH_BC_TASK = "reporting.check_bc_health"
register_dispatch_task(HEALTH_BC_TASK, "control")
HEALTH_CHUNK_SIZE = 64
HEALTH_MAX_CHUNKS = 16
HEALTH_CYCLE_SECONDS = 120
FRESHNESS_BATCH_SIZE = 512
COVERAGE_REFRESH = timedelta(minutes=3)
COVERAGE_MAX_AGE = timedelta(minutes=5)
COVERAGE_COUNTS = {"missing_directory_ads", "unlinked_material_posts"}
COVERAGE_ISSUES = {"COVERAGE_PENDING", "COVERAGE_STALE", "COVERAGE_QUERY_FAILED"}


class _HealthProgress(TypedDict):
    cycle: str
    scope: str
    at: str
    offset: int
    metrics: dict[str, Any]
    issues: list[str]
    repairs: dict[str, list[str]]
    batch_size: int
    success_streak: int
    continuation_seq: int
    retry_not_before: str | None


class _HealthBatchTimeout(Exception):
    """失败批已回滚并保存缩批信息；外层独立事务记录失败并安排有界续投。"""

    def __init__(self, cycle: str):
        self.cycle = cycle
        super().__init__("health observation batch timed out")


def _fresh_parents(session, params, *, kind, keys):
    """在 Python 去重，避免数据库低估当天事实后把父键回连变成平方扫描。"""
    fresh = set()
    # 缺失父 ID 仍由调用方计作缺口，不参与排序或主键探测。
    ordered = sorted(key for key in keys if key[1] is not None)
    for offset in range(0, len(ordered), 256):
        batch = ordered[offset : offset + 256]
        fresh.update(
            session.execute(
                text("""
            SELECT r.advertiser_id,r.parent_id
            FROM unnest(CAST(:advertisers AS text[]),CAST(:parents AS text[]))
              r(advertiser_id,parent_id)
            JOIN LATERAL (
              SELECT true FROM ad_object o
              WHERE o.tenant_id=:t AND o.advertiser_id=r.advertiser_id
                AND o.kind=:kind AND o.remote_id=r.parent_id
                AND o.observed_at >= CASE WHEN r.advertiser_id=ANY(:strict_accounts)
                  THEN :rule_cutoff ELSE :parent_cutoff END LIMIT 1
            ) found ON true
        """),
                {
                    **params,
                    "kind": kind,
                    "advertisers": [key[0] for key in batch],
                    "parents": [key[1] for key in batch],
                },
            ).all()
        )
    return fresh


def _missing_directory_ads(session, params):
    rows = (
        session.execute(
            text("""
        SELECT r.advertiser_id,r.ad_id,r.adgroup_id,r.campaign_id,a.fresh
        FROM ad_report_day r
        LEFT JOIN LATERAL (
          SELECT true AS fresh FROM ad_object o
          WHERE o.tenant_id=r.tenant_id AND o.advertiser_id=r.advertiser_id
            AND o.kind='ad' AND o.remote_id=r.ad_id
            AND o.observed_at >= CASE WHEN r.advertiser_id=ANY(:strict_accounts)
              THEN :rule_cutoff ELSE :status_cutoff END LIMIT 1
        ) a ON true
        WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids)
          AND r.stat_date BETWEEN :first_day AND :last_day
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND r.spend>0
    """),
            params,
        )
        .mappings()
        .all()
    )
    groups = _fresh_parents(
        session,
        params,
        kind="adgroup",
        keys={
            (row["advertiser_id"], row["adgroup_id"]) for row in rows if row["fresh"]
        },
    )
    campaigns = _fresh_parents(
        session,
        params,
        kind="campaign",
        keys={
            (row["advertiser_id"], row["campaign_id"])
            for row in rows
            if row["fresh"] and (row["advertiser_id"], row["adgroup_id"]) in groups
        },
    )
    return [
        row
        for row in rows
        if not row["fresh"]
        or (row["advertiser_id"], row["adgroup_id"]) not in groups
        or (row["advertiser_id"], row["campaign_id"]) not in campaigns
    ]


def _measurement_params(session, plan, *, now, owned, coverage):
    # 常驻连接的通用计划不知道BC账户数组长度，会放弃日期索引；按本轮参数
    # 规划，避免“重启后快、复用后慢”使健康检查超时。仅作用于当前事务。
    session.connection().execute(text("SET LOCAL plan_cache_mode='force_custom_plan'"))
    strict_accounts = (
        rule_accounts(
            session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=owned
        )
        if coverage
        else set()
    )
    return {
        "t": plan.tenant_id,
        "bc": plan.bc_id,
        "ids": owned,
        "active": []
        if coverage
        else sorted(
            set(plan.last_active_advertiser_ids)
            | known_reporting_accounts(session, plan, accounts=owned, now=now)
        ),
        "now": now,
        "cutoff": now - SLO,
        # 全球当地日只可能落在 UTC 前后一天；先走日期索引，再按事实时区精确判断。
        "first_day": now.astimezone(UTC).date() - timedelta(days=1),
        "last_day": now.astimezone(UTC).date() + timedelta(days=1),
        # 热报发布才规划目录，完成读取还需时间；健康期限要留执行余量，
        # 否则正常轮次会被当成缺口触发额外报表补拉，抵消降频。
        "status_cutoff": now - (STATUS_INTERVAL + REFRESH_MARGIN),
        "parent_cutoff": now - (PARENT_INTERVAL + REFRESH_MARGIN),
        "rule_cutoff": now
        - (directory_interval("ad", has_rules=True) + REFRESH_MARGIN),
        "strict_accounts": sorted(strict_accounts),
        "action_cutoff": now - timedelta(minutes=2),
    }


def measure_freshness(session, plan, *, now, accounts=None):
    """分钟检查只读发布水位、筛选与动作队列，不触碰目录或素材关联事实。"""
    owned = owned_accounts(session, plan) if accounts is None else accounts
    params = _measurement_params(session, plan, now=now, owned=owned, coverage=False)
    # 每账户合并今日/昨日两类水位，只按主键读取最多四行；聚合保留缺失的 NULL。
    # 不再让双 JOIN 在大池重复随机读取，或改走整租户历史水位的哈希扫描。
    # 一次求出完整活跃归属集合；只拆SQL执行，不降低覆盖、不分批发布。
    # 每句仍受原5秒限制，调用方10秒总预算覆盖全部批次与后续指标。
    active = sorted(set(owned) & set(params["active"]))
    rows = []
    for offset in range(0, len(active), FRESHNESS_BATCH_SIZE):
        rows.extend(
            session.execute(
                text("""
            WITH active AS (
              SELECT a.advertiser_id,a.timezone,(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date AS local_day
              FROM advertiser_account a WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
                AND a.advertiser_id=ANY(:active))
            SELECT a.advertiser_id,a.timezone,a.local_day,kinds.kind AS report_kind,
              kinds.last_success_at,kinds.previous_success_at
            FROM active a
            LEFT JOIN LATERAL (
              SELECT
                max(s.last_success_at) FILTER (WHERE s.report_kind='ad_day'
                  AND s.stat_date=a.local_day) AS ad_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='ad_day'
                  AND s.stat_date=a.local_day-1) AS ad_previous_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='material_day'
                  AND s.stat_date=a.local_day) AS material_at,
                max(s.last_success_at) FILTER (WHERE s.report_kind='material_day'
                  AND s.stat_date=a.local_day-1) AS material_previous_at
              FROM report_sync_state s
              WHERE s.tenant_id=:t AND s.advertiser_id=a.advertiser_id
                AND s.report_kind IN ('ad_day','material_day')
                AND s.stat_date BETWEEN a.local_day-1 AND a.local_day
            ) recent ON true
            CROSS JOIN LATERAL (VALUES
              ('ad_day',recent.ad_at,recent.ad_previous_at),
              ('material_day',recent.material_at,recent.material_previous_at)
            ) kinds(kind,last_success_at,previous_success_at)
        """),
                {**params, "ids": active[offset : offset + FRESHNESS_BATCH_SIZE]},
            )
            .mappings()
            .all()
        )
    stale = {"ad_day": set(), "material_day": set()}
    times = []
    for row in rows:
        at = fresh_report_success(
            {
                row["local_day"]: row["last_success_at"],
                row["local_day"] - timedelta(days=1): row["previous_success_at"],
            },
            timezone=row["timezone"],
            kind=row["report_kind"],
            now=now,
        )
        # 保留过期今日事实的年龄指标；跨日借用成功时则记录实际采用的时间。
        measured_at = at if at is not None else row["last_success_at"]
        if measured_at is not None:
            times.append(measured_at)
        if at is None:
            stale[row["report_kind"]].add(row["advertiser_id"])
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
        "pending_actions": pending,
        "oldest_data_at": min(times).isoformat() if times else None,
    }
    issues = [
        key for key, value in metrics.items() if key != "oldest_data_at" and value
    ]
    repairs = {
        "ad": stale["ad_day"],
        "material": stale["material_day"],
        "links": set(),
    }
    if owned and (
        plan.active_screened_at is None or plan.active_screened_at < now - SLO
    ):
        # 没有“完整筛选确认为零”的证据，空活跃集合是覆盖未知，不能报健康。
        issues.append("ACCOUNT_SCREEN_STALE")
    return metrics, issues, repairs


def measure_coverage(session, plan, *, now, accounts=None):
    """有时间标记的完整覆盖快照；分片期间不对外发布部分指标。"""
    owned = owned_accounts(session, plan) if accounts is None else accounts
    params = _measurement_params(session, plan, now=now, owned=owned, coverage=True)
    # 逐批完整测量；任一批失败仍向外抛出，不能把部分覆盖标记为健康。
    # 同批广告共享的父对象只读取一次；缺失/过期子层不再探测父层。
    # 用集合回连保留逐广告缺口数，避免数据库低估基数引起父链平方扫描。
    missing = []
    directory_accounts = sorted(set(owned))
    for offset in range(0, len(directory_accounts), 256):
        missing.extend(
            _missing_directory_ads(
                session,
                {**params, "ids": directory_accounts[offset : offset + 256]},
            )
        )
    # 素材关联同样逐账户主键前缀短探测，避免反连接扫描所有历史映射。
    unlinked = []
    for offset in range(0, len(directory_accounts), 256):
        unlinked.extend(
            session.execute(
                text("""
            WITH posts AS MATERIALIZED (
              SELECT DISTINCT r.advertiser_id,r.main_material_id FROM material_report_day r
              WHERE r.tenant_id=:t AND r.advertiser_id=ANY(:ids) AND r.spend>0
                AND r.stat_date BETWEEN :first_day AND :last_day
                AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
            )
            SELECT r.advertiser_id,r.main_material_id FROM posts r
            LEFT JOIN LATERAL (
              SELECT true AS found FROM material_post_link l
              WHERE l.tenant_id=:t AND l.advertiser_id=r.advertiser_id
                AND l.main_material_id=r.main_material_id
                AND l.platform_material_id IS NOT NULL LIMIT 1
            ) linked ON true
            WHERE linked.found IS NULL
        """),
                {**params, "ids": directory_accounts[offset : offset + 256]},
            )
            .mappings()
            .all()
        )
    metrics = {
        "missing_directory_ads": len(missing),
        "unlinked_material_posts": len(unlinked),
    }
    return (
        metrics,
        [key for key, value in metrics.items() if value],
        {
            "ad": {r["advertiser_id"] for r in missing},
            "material": set(),
            "links": {r["advertiser_id"] for r in unlinked},
        },
    )


def measure_health(session, plan, *, now, accounts=None):
    """需要单次完整观测的内部调用复用两种测量；周期任务分别调度。"""
    fresh, issues, repairs = measure_freshness(
        session, plan, now=now, accounts=accounts
    )
    coverage, coverage_issues, coverage_repairs = measure_coverage(
        session, plan, now=now, accounts=accounts
    )
    return (
        fresh | coverage,
        issues + coverage_issues,
        {key: repairs[key] | coverage_repairs[key] for key in repairs},
    )


def _rescue_queued_actions(session, plan, *, now):
    """只重投原本已授权且仍排队的任务正文；业务执行器继续负责幂等、权限和发送栅栏。"""
    params = {
        "t": plan.tenant_id,
        "bc": plan.bc_id,
        "cutoff": now - timedelta(minutes=2),
    }
    # 空业务队列先按租户/BC状态索引返回，避免规划器先全扫数百万历史投递；
    # 存在候选时仍从原消息取最新正文和操作者，保留恢复身份与幂等规则。
    pending = session.execute(
        text("""
        SELECT EXISTS(SELECT 1 FROM management_task
          WHERE tenant_id=:t AND bc_id=:bc AND status='QUEUED' AND created_at<:cutoff)
    """),
        params,
    ).scalar_one()
    if not pending:
        return 0
    rows = session.execute(
        text("""
        SELECT DISTINCT ON (m.id) p.actor_id,p.task_name,p.payload,m.id
        FROM management_task m JOIN pending_dispatch p ON p.tenant_id=m.tenant_id
          AND p.task_name='ad_management.execute' AND p.payload->>'task_id'=m.id::text
        WHERE m.tenant_id=:t AND m.bc_id=:bc AND m.status='QUEUED' AND m.created_at<:cutoff
        ORDER BY m.id,p.available_at DESC LIMIT 50
    """),
        params,
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


def coverage_issues(metrics, *, now, scope=None):
    """完整覆盖是最多五分钟的观测，不用分钟水位伪造其观测时间。"""
    issues = [name for name in sorted(COVERAGE_COUNTS) if metrics.get(name)]
    if scope is not None and metrics.get("coverage_scope") != scope:
        return issues + ["COVERAGE_PENDING"]
    status = metrics.get("coverage_status", "PENDING")
    if status == "FAILED":
        return issues + ["COVERAGE_QUERY_FAILED"]
    if status != "COMPLETE" or not metrics.get("coverage_observed_at"):
        return issues + ["COVERAGE_PENDING"]
    if (
        datetime.fromisoformat(metrics["coverage_observed_at"])
        <= now - COVERAGE_MAX_AGE
    ):
        issues.append("COVERAGE_STALE")
    return issues


def _locked_health(session, plan):
    # 两阶段只在合并时锁行；重新加载最新值，不能拿测量前的 ORM 快照覆盖。
    session.connection().execute(
        text("""
        INSERT INTO report_sync_health(tenant_id,bc_id,status,metrics,issues,observed_at)
        VALUES (:tenant,:bc,'DEGRADED','{}','[]',:unobserved)
        ON CONFLICT(tenant_id,bc_id) DO NOTHING
        """),
        {
            "tenant": plan.tenant_id,
            "bc": plan.bc_id,
            "unobserved": datetime(1970, 1, 1, tzinfo=UTC),
        },
    )
    return session.exec(
        select(ReportSyncHealth)
        .where(
            ReportSyncHealth.tenant_id == plan.tenant_id,
            ReportSyncHealth.bc_id == plan.bc_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def observe_health(
    session, plan, *, now, deadline=None, measurement=None, phase="full", scope=None
):
    remaining = (
        10
        if deadline is None
        else max(0, (deadline - datetime.now(UTC)).total_seconds())
    )
    if measurement is None:
        with query_budget(session, seconds=remaining):
            measurement = measure_health(session, plan, now=now)
    metrics, issues, repairs = measurement
    row = _locked_health(session, plan)
    merged = dict(row.metrics)
    previous_coverage = merged.get("coverage_observed_at")
    if phase in {"full", "coverage"}:
        if previous_coverage and datetime.fromisoformat(previous_coverage) > now:
            return row
        merged.update({key: metrics[key] for key in COVERAGE_COUNTS})
        merged.update(
            coverage_observed_at=now.isoformat(),
            coverage_completed_at=datetime.now(UTC).isoformat(),
            coverage_scope=scope,
            coverage_status="COMPLETE",
        )
    if phase in {"full", "freshness"}:
        if row.observed_at > now:
            return row
        merged.update(
            {key: value for key, value in metrics.items() if key not in COVERAGE_COUNTS}
        )
        merged.setdefault("coverage_observed_at", None)
        merged.setdefault("coverage_completed_at", None)
        merged.setdefault("coverage_scope", None)
        merged.setdefault("coverage_status", "PENDING")
        if phase == "freshness" and merged["coverage_scope"] != scope:
            merged["coverage_status"] = "PENDING"
            merged["coverage_scope"] = scope
        row.observed_at = now
        fresh_issues = [issue for issue in issues if issue not in COVERAGE_COUNTS]
    else:
        fresh_issues = [
            issue
            for issue in row.issues
            if issue not in COVERAGE_COUNTS | COVERAGE_ISSUES
        ]
        if row.observed_at < datetime.now(UTC) - timedelta(minutes=3):
            fresh_issues.append("MONITOR_STALE")
    issues = sorted(
        set(
            fresh_issues
            + coverage_issues(merged, now=max(now, row.observed_at), scope=scope)
        )
    )
    metrics = merged
    status = "DEGRADED" if issues else "HEALTHY"
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
    row.status, row.metrics, row.issues = status, metrics, issues
    repair_at = datetime.now(UTC) if phase == "coverage" else now
    # 完整覆盖每轮最多一次；分钟修复不能用共享 last_repair_at 长期压住关联补齐。
    if issues and (
        phase == "coverage"
        or row.last_repair_at is None
        or row.last_repair_at <= repair_at - REPAIR_INTERVAL
    ):
        # 观测先落在外层事务；补任务遇到规划锁时仅回滚补任务，不丢失健康事实。
        session.flush()
        try:
            with session.begin_nested():
                previous = (
                    session.connection().execute(text("SHOW lock_timeout")).scalar_one()
                )
                session.connection().execute(text("SET LOCAL lock_timeout='100ms'"))
                with query_budget(session, seconds=REPAIR_BUDGET_SECONDS):
                    _repair_health(
                        session,
                        plan,
                        row,
                        repairs,
                        repair_at,
                        screen_unknown=phase in {"full", "freshness"}
                        and "ACCOUNT_SCREEN_STALE" in fresh_issues,
                    )
                    session.flush()
                session.connection().execute(
                    text("SELECT set_config('lock_timeout', :value, true)"),
                    {"value": previous},
                )
        except (DBAPIError, QueryBudgetExceeded) as error:
            if isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) not in {"55P03", "57014"}:
                raise
            logger.warning(
                "sync health repair deferred: tenant=%s bc=%s",
                plan.tenant_id,
                plan.bc_id,
            )
    session.flush()
    return row


def _repair_fact_accounts(session, plan, accounts, *, kind, now):
    """在途账户不占救援名额；不同当地日合计仍只补最老的50个缺口。"""
    candidates = []
    for (start, end), ids in _group_by_window(
        session, plan, sorted(accounts), lambda tz: _hot_window(tz, now)
    ).items():
        params = {
            "tenant": plan.tenant_id,
            "bc": plan.bc_id,
            "actor": plan.actor_id,
            "route": json.dumps(plan.frozen_route),
            "kind": kind,
            "start": start,
            "end": end,
        }
        # 与enqueue_jobs的实时覆盖身份一致，先整批读取一次，避免逐账户扫任务。
        # 此处只筛候选，最终入队仍由原覆盖事务锁与唯一键串行复核竞争。
        pending = (
            session.connection()
            .execute(
                text("""
            SELECT advertiser_ids FROM report_sync_job
            WHERE tenant_id=:tenant AND bc_id=:bc AND actor_id=:actor
              AND frozen_route=CAST(:route AS jsonb) AND kind=:kind
              AND priority<=2 AND start_date=:start AND end_date=:end
              AND campaign_ids='[]'::jsonb AND status IN ('QUEUED','RUNNING','WAITING')
            """),
                params,
            )
            .scalars()
        )
        covered = {advertiser for group in pending for advertiser in group}
        remaining = [advertiser for advertiser in ids if advertiser not in covered]
        if not remaining:
            continue
        candidates.extend(
            session.connection()
            .execute(
                text("""
                SELECT a.advertiser_id,s.last_success_at
                FROM unnest(CAST(:ids AS text[])) a(advertiser_id)
                LEFT JOIN LATERAL (
                  SELECT last_success_at FROM report_sync_state s
                  WHERE s.tenant_id=:tenant AND s.advertiser_id=a.advertiser_id
                    AND s.report_kind=:kind AND s.stat_date=:start LIMIT 1
                ) s ON true
                ORDER BY s.last_success_at ASC NULLS FIRST,a.advertiser_id LIMIT :limit
                """),
                params | {"ids": remaining, "limit": MAX_REPAIR_ACCOUNTS},
            )
            .all()
        )
    # 每个日期先取50足以覆盖全局前50；不因多时区放大修复上限。
    candidates.sort(
        key=lambda row: (
            row.last_success_at is not None,
            row.last_success_at or datetime.min.replace(tzinfo=UTC),
            row.advertiser_id,
        )
    )
    return [row.advertiser_id for row in candidates[:MAX_REPAIR_ACCOUNTS]]


def _repair_health(session, plan, row, repairs, now, *, screen_unknown=False):
    # 未知账户选择只决定补修，不是健康指标；必须留在修复保存点与2秒预算内。
    # 覆盖续步不重复筛选，分钟/full阶段才按当前筛选告警补最旧的50个账户。
    if screen_unknown:
        unknown = session.execute(
            text("""
            SELECT a.advertiser_id FROM advertiser_account a
            LEFT JOIN report_sync_state s ON s.tenant_id=a.tenant_id AND s.advertiser_id=a.advertiser_id
              AND s.report_kind='ad_day' AND s.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE a.timezone)::date
            WHERE a.tenant_id=:t AND a.advertiser_id=ANY(:ids)
              AND (s.last_success_at IS NULL OR s.last_success_at<:cutoff)
            ORDER BY s.last_success_at ASC NULLS FIRST, a.advertiser_id LIMIT :limit
        """),
            {
                "t": plan.tenant_id,
                "ids": owned_accounts(session, plan),
                "now": now,
                "cutoff": now - SLO,
                "limit": MAX_REPAIR_ACCOUNTS,
            },
        ).scalars()
        repairs = {**repairs, "ad": repairs["ad"] | set(unknown)}
    count = plan_hot(
        session,
        plan,
        active=_repair_fact_accounts(
            session, plan, repairs["ad"], kind="ad_day", now=now
        ),
        now=now,
    )
    count += plan_material(
        session,
        plan,
        active=_repair_fact_accounts(
            session, plan, repairs["material"], kind="material_day", now=now
        ),
        now=now,
    )
    for (start, end), ids in _group_by_window(
        session,
        plan,
        sorted(repairs["links"])[:MAX_REPAIR_ACCOUNTS],
        lambda tz: _hot_window(tz, now),
    ).items():
        # 已有同冻结范围、同当前日窗口的待领工作保留原退避；证据变化交给
        # wake_link_dependencies复核，健康告警本身不是重新扫描素材的证据。
        covered = set(
            session.connection()
            .execute(
                text("""
            SELECT a.advertiser_id FROM unnest(CAST(:ids AS text[])) a(advertiser_id)
            JOIN LATERAL (
              SELECT j.id FROM report_sync_job j
              WHERE j.tenant_id=:tenant AND j.bc_id=:bc AND j.actor_id=:actor
                AND j.frozen_route=CAST(:route AS jsonb)
                AND j.kind='material_links' AND j.status IN ('QUEUED','WAITING')
                AND j.advertiser_ids=jsonb_build_array(a.advertiser_id)
                AND j.campaign_ids='[]'::jsonb AND j.priority<=2
                AND j.start_date=:start AND j.end_date=:end
                AND (j.claimed_until IS NULL OR j.claimed_until<=:now)
              LIMIT 1 FOR UPDATE SKIP LOCKED
            ) pending ON true
        """),
                {
                    "tenant": plan.tenant_id,
                    "bc": plan.bc_id,
                    "actor": plan.actor_id,
                    "route": json.dumps(plan.frozen_route),
                    "ids": ids,
                    "start": start,
                    "end": end,
                    "now": now,
                },
            )
            .scalars()
        )
        for advertiser in ids:
            if advertiser in covered:
                continue
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


def schedule_health(*, database_engine, now=None):
    """Beat 每分钟只登记独立 BC 任务，不把后续 BC 塞进前一个的剩余预算。"""
    if not settings.ADS_SYNC_ENABLED:
        return 0
    now = now or datetime.now(UTC)
    count = 0
    with Session(database_engine) as session:
        keys = session.exec(
            select(AdSyncBc.tenant_id, AdSyncBc.bc_id).where(
                col(AdSyncBc.enabled).is_(True)
            )
        ).all()
    for key in keys:
        try:
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if plan is None or not plan.enabled:
                    continue
                enqueue_after_commit(
                    session,
                    context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
                    task_name=HEALTH_BC_TASK,
                    task_key=f"sync-health-measure:{plan.bc_id}:{int(now.timestamp()) // 60}",
                    payload={"bc_id": plan.bc_id},
                )
                count += 1
        except DBAPIError, DomainError:
            # 单 BC 写入失败不挡后续 BC；Celery 软期限仍向外传播结束本轮。
            logger.exception("sync health dispatch failed tenant=%s bc=%s", *key)
    return count


def _health_lock_key(tenant_id, bc_id):
    return f"reporting:health:bc:v1:{tenant_id}:{bc_id}"


def _health_progress_key(tenant_id, bc_id):
    return f"reporting:health:progress:v2:{tenant_id}:{bc_id}"


def _health_scope(session, plan, owned, *, now=None):
    now = now or datetime.now(UTC)
    # 轻量范围元数据能使时区、跨日、规则阈值变化失效；筛选时间不改变覆盖范围。
    zones = (
        session.connection()
        .execute(
            text("""
        SELECT advertiser_id,timezone,(CAST(:now AS timestamptz) AT TIME ZONE timezone)::date
        FROM advertiser_account WHERE tenant_id=:tenant AND advertiser_id=ANY(:ids)
        ORDER BY advertiser_id
    """),
            {"tenant": plan.tenant_id, "ids": sorted(owned), "now": now},
        )
        .all()
    )
    strict = rule_accounts(
        session, tenant_id=plan.tenant_id, bc_id=plan.bc_id, accounts=owned
    )
    return sha256(
        json.dumps(
            [
                str(plan.tenant_id),
                plan.bc_id,
                str(plan.actor_id),
                plan.frozen_route,
                sorted(owned),
                [[account, zone, day.isoformat()] for account, zone, day in zones],
                sorted(strict),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _new_coverage_progress(scope):
    return {
        "cycle": uuid4().hex,
        "scope": scope,
        "at": datetime.now(UTC).isoformat(),
        "offset": 0,
        "metrics": {},
        "issues": [],
        "repairs": {"ad": [], "material": [], "links": []},
        "batch_size": HEALTH_CHUNK_SIZE,
        "success_streak": 0,
        "continuation_seq": 0,
        "retry_not_before": None,
    }


def _schedule_coverage(session, plan, redis_client, *, row, scope, now):
    key = _health_progress_key(plan.tenant_id, plan.bc_id)
    raw = redis_client.get(key)
    progress = json.loads(raw) if raw else None
    if progress is None or progress["scope"] != scope:
        observed = row.metrics.get("coverage_observed_at")
        if (
            row.metrics.get("coverage_status") == "COMPLETE"
            and row.metrics.get("coverage_scope") == scope
            and observed
            and datetime.fromisoformat(observed) > now - COVERAGE_REFRESH
        ):
            return "COMPLETE"
        progress = _new_coverage_progress(scope)
    if progress.get("retry_not_before") and now < datetime.fromisoformat(
        progress["retry_not_before"]
    ):
        return "WAIT"
    _enqueue_health_continuation(session, plan, redis_client, progress)
    return "CONTINUE"


def _enqueue_health_continuation(
    session, plan, redis_client, progress: _HealthProgress
):
    # 同 offset 的失败重试也必须有新投递键；原 outbox 已发布不能吞掉下一次唤醒。
    progress["continuation_seq"] += 1
    redis_client.set(
        _health_progress_key(plan.tenant_id, plan.bc_id), json.dumps(progress), ex=180
    )
    enqueue_after_commit(
        session,
        context=TenantContext(plan.tenant_id, plan.actor_id, "operator"),
        task_name=HEALTH_BC_TASK,
        task_key=f"sync-health-continue:{plan.bc_id}:{progress['cycle']}:{progress['offset']}:{progress['continuation_seq']}",
        payload={"bc_id": plan.bc_id, "cycle": progress["cycle"]},
    )


def _advance_health(session, plan, redis_client, *, cycle, deadline):
    """分片只保存私有进度；完整范围测完后才发布健康事实和执行修复。"""
    key = _health_progress_key(plan.tenant_id, plan.bc_id)
    raw = redis_client.get(key)
    progress: _HealthProgress | None = json.loads(raw) if raw else None
    # 旧续投不能新建或推进后来的一轮；Beat 的无 token 任务可接续崩溃进度。
    if cycle is not None and (not progress or progress["cycle"] != cycle):
        return "IGNORED"
    owned = sorted(set(owned_accounts(session, plan)))
    scope = _health_scope(session, plan, owned)
    if progress and datetime.now(UTC) - datetime.fromisoformat(
        progress["at"]
    ) > timedelta(seconds=HEALTH_CYCLE_SECONDS):
        redis_client.delete(key)
        raise QueryBudgetExceeded("health observation cycle expired")
    if progress is None or progress["scope"] != scope:
        redis_client.delete(key)
        return "IGNORED"
    # v2 只累计覆盖指标，与旧的全指标进度隔离。
    progress.setdefault("batch_size", HEALTH_CHUNK_SIZE)
    progress.setdefault("success_streak", 0)
    progress.setdefault("continuation_seq", 0)
    progress.setdefault("retry_not_before", None)
    if progress["retry_not_before"] and datetime.now(UTC) < datetime.fromisoformat(
        progress["retry_not_before"]
    ):
        return "WAIT"
    as_of = datetime.fromisoformat(progress["at"])
    total = max(1, len(owned))
    for _ in range(HEALTH_MAX_CHUNKS):
        if progress["offset"] >= total or deadline - monotonic() < 3:
            break
        end = min(total, progress["offset"] + progress["batch_size"])
        # SAVEPOINT 只包本批；失败时已成功批次仍可从 Redis 恢复，不跳过失败账户。
        batch_started = monotonic()
        try:
            with session.begin_nested(), query_budget(session, seconds=3):
                metrics, issues, repairs = measure_coverage(
                    session, plan, now=as_of, accounts=owned[progress["offset"] : end]
                )
        except (DBAPIError, QueryBudgetExceeded) as error:
            if (
                isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) != "57014"
            ):
                raise
            attempted = end - progress["offset"]
            progress["batch_size"] = max(1, attempted // 2)
            progress["success_streak"] = 0
            # 单账户已无法再拆；不跳账户，也不让重复消息绕过冷却形成忙循环。
            progress["retry_not_before"] = (
                (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
                if attempted == 1
                else None
            )
            redis_client.set(key, json.dumps(progress), ex=180)
            raise _HealthBatchTimeout(progress["cycle"]) from error
        batch_seconds = monotonic() - batch_started
        accumulated = progress["metrics"]
        for name, value in metrics.items():
            if name == "pending_actions":
                # 这是 BC 级计数，采用最后一次完整查询，不能按账户批次相加。
                accumulated[name] = value
            elif name == "oldest_data_at":
                times = [at for at in (accumulated.get(name), value) if at]
                accumulated[name] = min(times) if times else None
            else:
                accumulated[name] = accumulated.get(name, 0) + value
        progress["issues"] = sorted(
            set(progress["issues"])
            | {issue for issue in issues if issue not in metrics}
        )
        for name, accounts in repairs.items():
            progress["repairs"][name] = sorted(
                set(progress["repairs"][name]) | accounts
            )
        progress["offset"] = end
        progress["retry_not_before"] = None
        # 成功不等于仍有扩批余量；贴近3秒期限的批次继续倍增会反复取消并重读。
        # 慢成功已计入完整进度，只缩小下一批；仅连续两批不超过1秒才恢复扩批。
        if batch_seconds > 1.5:
            progress["batch_size"] = max(1, progress["batch_size"] // 2)
        progress["success_streak"] = (
            progress["success_streak"] + 1 if batch_seconds <= 1 else 0
        )
        if progress["success_streak"] >= 2:
            progress["batch_size"] = min(HEALTH_CHUNK_SIZE, progress["batch_size"] * 2)
            progress["success_streak"] = 0
        redis_client.set(key, json.dumps(progress), ex=180)
    if progress["offset"] < total:
        _enqueue_health_continuation(session, plan, redis_client, progress)
        return "CONTINUE"
    # 观测期间授权或计划范围变化，不能发布旧范围；下次 Beat 从新范围完整重测。
    session.refresh(plan)
    current_owned = sorted(set(owned_accounts(session, plan)))
    if not plan.enabled or _health_scope(session, plan, current_owned) != scope:
        redis_client.delete(key)
        return "IGNORED"
    require_tenant(
        session,
        actor_id=plan.actor_id,
        tenant_id=plan.tenant_id,
        action="reporting_write",
    )
    if datetime.now(UTC) - as_of > timedelta(seconds=HEALTH_CYCLE_SECONDS):
        redis_client.delete(key)
        raise QueryBudgetExceeded("health observation cycle expired")
    metrics = progress["metrics"]
    issues = [
        name for name, value in metrics.items() if name != "oldest_data_at" and value
    ] + progress["issues"]
    repairs = {name: set(accounts) for name, accounts in progress["repairs"].items()}
    observe_health(
        session,
        plan,
        now=as_of,
        measurement=(metrics, issues, repairs),
        phase="coverage",
        scope=scope,
    )
    return "COMPLETE"


def run_bc_health(*, database_engine, redis_client, context, bc_id, cycle=None):
    if not settings.ADS_SYNC_ENABLED:
        return "IGNORED"
    key = (context.tenant_id, bc_id)
    lock = redis_client.lock(_health_lock_key(*key), timeout=30, blocking=False)
    if not lock.acquire(blocking=False):
        return "BUSY"
    try:
        now = datetime.now(UTC)
        deadline = monotonic() + 10
        try:
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if plan is None or not plan.enabled:
                    return "IGNORED"
                if plan.actor_id != context.actor_id:
                    raise DomainError(
                        "dispatch_payload_invalid", "健康任务与当前计划身份不符"
                    )
                require_tenant(
                    session,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    action="reporting_write",
                )
                # 使用执行时刻与当前计划；不携带过期冻结路由，也不共享别的 BC 的时限。
                if cycle is None:
                    with query_budget(session, seconds=max(0, deadline - monotonic())):
                        owned = sorted(set(owned_accounts(session, plan)))
                        scope = _health_scope(session, plan, owned, now=now)
                        measurement = measure_freshness(
                            session, plan, now=now, accounts=owned
                        )
                        session.refresh(plan)
                        if (
                            not plan.enabled
                            or plan.actor_id != context.actor_id
                            or _health_scope(
                                session, plan, owned_accounts(session, plan)
                            )
                            != scope
                        ):
                            return "IGNORED"
                        require_tenant(
                            session,
                            actor_id=context.actor_id,
                            tenant_id=context.tenant_id,
                            action="reporting_write",
                        )
                    row = observe_health(
                        session,
                        plan,
                        now=now,
                        measurement=measurement,
                        phase="freshness",
                        scope=scope,
                    )
                    result = _schedule_coverage(
                        session, plan, redis_client, row=row, scope=scope, now=now
                    )
                else:
                    result = _advance_health(
                        session, plan, redis_client, cycle=cycle, deadline=deadline
                    )
            if result == "COMPLETE" and cycle is not None:
                # 数据库提交成功后才删除进度；提交失败/进程崩溃可重试完整发布。
                redis_client.delete(_health_progress_key(*key))
            return result
        except DomainError:
            raise
        except Exception as error:
            logger.exception("sync health check failed tenant=%s bc=%s", *key)
            # 失败不覆盖上次完整指标；只改变本 BC 的失败可见状态。
            with Session(database_engine) as session, session.begin():
                session.connection().execute(text("SET LOCAL statement_timeout='5s'"))
                plan = session.get(AdSyncBc, key)
                if (
                    plan is None
                    or not plan.enabled
                    or plan.actor_id != context.actor_id
                ):
                    return "IGNORED"
                row = _locked_health(session, plan)
                if cycle is not None:
                    row.metrics = {**row.metrics, "coverage_status": "FAILED"}
                    fresh_issues = [
                        issue
                        for issue in row.issues
                        if issue not in COVERAGE_COUNTS | COVERAGE_ISSUES
                    ]
                    row.issues = sorted(
                        set(fresh_issues + coverage_issues(row.metrics, now=now))
                    )
                else:
                    row.issues = sorted(
                        set(
                            ["MONITOR_QUERY_FAILED"]
                            + coverage_issues(row.metrics, now=now)
                        )
                    )
                # 失败不推进任何成功观测时间，分钟失败与覆盖失败不能互相清除。
                row.status = "DEGRADED"
                if cycle is None:
                    # 分钟水位失败不能阻止到期的覆盖观测。失败事实先留在外层，
                    # 授权/范围读取或outbox登记再失败，只回滚此处独立保存点。
                    session.flush()
                    try:
                        with session.begin_nested(), query_budget(session, seconds=2):
                            scope = _health_scope(
                                session, plan, owned_accounts(session, plan)
                            )
                            session.refresh(plan)
                            if (
                                plan.enabled
                                and plan.actor_id == context.actor_id
                                and _health_scope(
                                    session, plan, owned_accounts(session, plan)
                                )
                                == scope
                            ):
                                require_tenant(
                                    session,
                                    actor_id=context.actor_id,
                                    tenant_id=context.tenant_id,
                                    action="reporting_write",
                                )
                                _schedule_coverage(
                                    session,
                                    plan,
                                    redis_client,
                                    row=row,
                                    scope=scope,
                                    now=datetime.now(UTC),
                                )
                                session.flush()
                    except Exception:
                        logger.exception(
                            "sync coverage dispatch deferred after freshness failure"
                        )
                if isinstance(error, _HealthBatchTimeout):
                    raw = redis_client.get(_health_progress_key(*key))
                    progress: _HealthProgress | None = json.loads(raw) if raw else None
                    if (
                        progress is not None
                        and progress["cycle"] == error.cycle
                        and progress["retry_not_before"] is None
                        and datetime.now(UTC) - datetime.fromisoformat(progress["at"])
                        < timedelta(seconds=HEALTH_CYCLE_SECONDS)
                        and _health_scope(session, plan, owned_accounts(session, plan))
                        == progress["scope"]
                    ):
                        require_tenant(
                            session,
                            actor_id=context.actor_id,
                            tenant_id=context.tenant_id,
                            action="reporting_write",
                        )
                        # 一次任务只缩小一次即让出；失败事实与下一次投递在同事务提交。
                        _enqueue_health_continuation(
                            session, plan, redis_client, progress
                        )
            return "FAILED"
    finally:
        lock.release()


@celery_app.task(name="reporting.check_health", time_limit=25, soft_time_limit=20)
def check_health():
    return schedule_health(database_engine=engine)


@celery_app.task(name=HEALTH_BC_TASK, time_limit=25, soft_time_limit=20)
def check_bc_health(*, tenant_id, actor_id, payload):
    validate_dispatch_payload(payload)
    if (
        set(payload) not in ({"bc_id"}, {"bc_id", "cycle"})
        or not isinstance(payload["bc_id"], str)
        or not payload["bc_id"]
        or (
            "cycle" in payload
            and (not isinstance(payload["cycle"], str) or not payload["cycle"])
        )
    ):
        raise DomainError("dispatch_payload_invalid", "健康任务参数无效")
    context = TenantContext(UUID(tenant_id), UUID(actor_id), "operator")
    with Redis.from_url(settings.REDIS_URL) as redis:
        return run_bc_health(
            database_engine=engine,
            redis_client=redis,
            context=context,
            bc_id=payload["bc_id"],
            cycle=payload.get("cycle"),
        )
