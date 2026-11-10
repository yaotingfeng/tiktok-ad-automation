"""分阶段上线的显式运维入口：本地身份回填、成对历史回补与只读对账。

不直接读写平台；回补进入现有冻结授权、限流和租约保护的报表队列。
"""

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

from app import models as _models  # noqa: F401 - CLI先注册模型，避免业务模块循环导入
from app.core.context import TenantContext
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.planner import owned_accounts
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc


def _plan(session, tenant_id, bc_id):
    plan = session.get(AdSyncBc, (tenant_id, bc_id))
    if plan is None or not plan.enabled:
        raise ValueError("active official BC sync plan required")
    return plan


def _backfill_windows(session, *, tenant_id, accounts, start, end, recent_start):
    """倒序七日段，每次最多32账户×7日期；新近日期跨账户优先。"""
    right = end
    while right >= start:
        left = max(start, right - timedelta(days=6))
        if left < recent_start <= right:
            left = recent_start
        for offset in range(0, len(accounts), 32):
            rows = (
                session.connection()
                .execute(
                    text("""SELECT DISTINCT advertiser_id,stat_date FROM ad_report_day
                WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
                  AND stat_date BETWEEN :start AND :end ORDER BY advertiser_id,stat_date"""),
                    {
                        "tenant": tenant_id,
                        "accounts": accounts[offset : offset + 32],
                        "start": left,
                        "end": right,
                    },
                )
                .all()
            )
            by_account = defaultdict(list)
            for row in rows:
                by_account[row.advertiser_id].append(row.stat_date)
            for account, days in by_account.items():
                first = last = days[0]
                for day in days[1:]:
                    if day != last + timedelta(days=1):
                        yield account, first, last
                        first = day
                    last = day
                yield account, first, last
        right = left - timedelta(days=1)


def enqueue_backfill(
    session: Session,
    *,
    tenant_id: UUID,
    bc_id: str,
    start: date,
    end: date,
    now: datetime,
    receipt: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """运维生产者独占此 Session：每账户/连续七日窗口提交，重跑复用既有需求。

    每笔事务至多创建2任务、14日需求；已有较宽冻结任务仍安全复用而不改写。
    receipt 在提交后发出，可用相同区间重启；中断仅回滚当前批次。
    """
    from app.modules.accounts.routing import verify_route

    if end < start:
        raise ValueError("invalid backfill date range")
    plan = _plan(session, tenant_id, bc_id)
    accounts = owned_accounts(session, plan)
    original_route = dict(plan.frozen_route)
    count = batches = 0
    for account, left, right in _backfill_windows(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        start=start,
        end=end,
        recent_start=now.date() - timedelta(days=35),
    ):
        try:
            # 每次写入边界重读计划和归属；局部行锁只保持到本窗口提交。
            plan = session.exec(
                select(AdSyncBc)
                .where(AdSyncBc.tenant_id == tenant_id, AdSyncBc.bc_id == bc_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if plan is None or not plan.enabled or plan.frozen_route != original_route:
                raise ValueError(
                    "backfill plan or frozen route changed; restart with current scope"
                )
            owner = session.exec(
                select(AdSyncAccount)
                .where(
                    AdSyncAccount.tenant_id == tenant_id,
                    AdSyncAccount.advertiser_id == account,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if owner is None or owner.owner_bc_id != bc_id:
                raise ValueError(
                    "backfill account ownership changed; restart with current scope"
                )
            verify_route(
                session,
                context=TenantContext(
                    tenant_id=tenant_id, actor_id=plan.actor_id, role="operator"
                ),
                route=FrozenTikTokRoute.model_validate(plan.frozen_route),
                advertiser_id=account,
                capability="read",
            )
            created = []
            for kind in ("ad_day", "creative_day"):
                job_id = enqueue_job(
                    session,
                    tenant_id=tenant_id,
                    bc_id=bc_id,
                    kind=kind,
                    tier="history",
                    advertiser_ids=[account],
                    start=left,
                    end=right,
                    frozen_route=plan.frozen_route,
                    actor_id=plan.actor_id,
                    requested_at=now,
                    execution_window_days=7,
                )
                if job_id:
                    created.append(str(job_id))
            session.commit()
        except Exception:
            session.rollback()
            raise
        count += len(created)
        batches += 1
        if receipt:
            receipt(
                {
                    "status": "COMMITTED",
                    "tenant": str(tenant_id),
                    "bc": bc_id,
                    "advertiser": account,
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "created_jobs": created,
                    "batches": batches,
                    "enqueued": count,
                }
            )
    return count


def audit(session, *, tenant_id, bc_id, start, end):
    """按币种、账户日时区分别审计；部分和只作排查证据，不充当完整总额。

    返回各源账户日桶数、未知指标行数和投影 PARTIAL/STALE/脏桶数；
    不按全 BC 展开日明细，也不将不同来源或未关联金额加进广告总账。
    """
    plan = _plan(session, tenant_id, bc_id)
    params = {
        "tenant": tenant_id,
        "accounts": owned_accounts(session, plan),
        "start": start,
        "end": end,
    }
    # 表名、指标与类别都是内部常量；作用域参数仍使用 SQL 绑定。
    scoped = """tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
        AND stat_date BETWEEN :start AND :end"""
    sources = []
    for prefix, table in (("ad", "ad_report_day"), ("creative", "creative_report_day")):
        sources.append(f"""
            SELECT advertiser_id,stat_date,currency,timezone,'{prefix}' AS source,
              spend,revenue_total,spend IS NULL AS spend_unknown,
              revenue_total IS NULL AS revenue_unknown,NULL::text AS source_status
            FROM {table} WHERE {scoped}
        """)
    sources.append(f"""
        SELECT advertiser_id,stat_date,currency,timezone,
          CASE attribution_status WHEN 'UNMAPPED' THEN 'unmapped'
            WHEN 'RESIDUAL' THEN 'residual' ELSE 'mapped' END AS source,
          spend,revenue_total,(spend IS NULL OR spend_has_null) AS spend_unknown,
          (revenue_total IS NULL OR revenue_total_has_null) AS revenue_unknown,source_status
        FROM creative_material_entity_day WHERE {scoped}
    """)
    aggregates = []
    for prefix in ("ad", "creative", "unmapped", "residual"):
        condition = f"source='{prefix}'"
        aggregates.extend(
            [
                f"count(*) FILTER(WHERE {condition}) AS {prefix}_rows",
                f"count(DISTINCT (s.advertiser_id,s.stat_date)) "
                f"FILTER(WHERE {condition}) AS {prefix}_buckets",
            ]
        )
        for column, metric in (("spend", "spend"), ("revenue_total", "revenue")):
            unknown = f"{metric}_unknown"
            # SUM 本身会忽略 NULL：完整值必须额外检查所有行的未知标志。
            # 无事实时保留 NULL；partial 只返回可见数值的和，不伪造零。
            aggregates.extend(
                [
                    f"CASE WHEN count(*) FILTER(WHERE {condition} AND {unknown})=0 "
                    f"THEN sum({column}) FILTER(WHERE {condition}) END AS {prefix}_{metric}",
                    f"sum({column}) FILTER(WHERE {condition}) AS {prefix}_{metric}_partial",
                    f"count(*) FILTER(WHERE {condition} AND {unknown}) "
                    f"AS {prefix}_{metric}_unknown_rows",
                ]
            )
    dirty_sql = """SELECT advertiser_id,bucket FROM read_bucket_state
        WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
          AND kind='creative_material' AND bucket BETWEEN :start AND :end
          AND source_revision>derived_revision"""
    rows = (
        session.connection()
        .execute(
            text(f"""
            WITH sources AS ({" UNION ALL ".join(sources)}), dirty AS ({dirty_sql})
            SELECT s.currency,s.timezone,{",".join(aggregates)},
              count(*) FILTER(WHERE source_status='PARTIAL') AS partial_rows,
              count(*) FILTER(WHERE source_status='STALE') AS stale_rows,
              count(DISTINCT (s.advertiser_id,s.stat_date))
                FILTER(WHERE d.advertiser_id IS NOT NULL) AS dirty_buckets
            FROM sources s LEFT JOIN dirty d
              ON d.advertiser_id=s.advertiser_id AND d.bucket=s.stat_date
            GROUP BY s.currency,s.timezone ORDER BY s.currency,s.timezone
        """),
            params,
        )
        .mappings()
        .all()
    )
    # 删除后的脏桶可能已没有任何事实；独立计数仍能暴露尚待重建的桶。
    dirty = (
        session.connection()
        .execute(text(f"SELECT count(*) FROM ({dirty_sql}) d"), params)
        .scalar_one()
    )
    return {"groups": [dict(row) for row in rows], "dirty": dirty}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("identities", "enqueue", "audit", "project"))
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--bc", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if not os.environ.get("APP_ENV_FILE"):
        parser.error("APP_ENV_FILE must explicitly select the environment")
    from app.core.db import engine
    from app.modules.ads.creatives import backfill_creatives
    from app.modules.reporting.read_models.dirty_buckets import ReadBucketKey
    from app.modules.reporting.read_models.projector import project_bucket

    with Session(engine) as session:
        plan = _plan(session, args.tenant, args.bc)
        accounts = owned_accounts(session, plan)
    if args.action == "identities":
        count = 0
        # 每账户单独提交，既有证据重放幂等；避免跨整个租户的大事务。
        for account in accounts:
            with Session(engine) as session:
                count += backfill_creatives(
                    session, tenant_id=args.tenant, advertiser_id=account
                )
                session.commit()
        result = {"identities": count}
    elif args.action == "project":
        count = 0
        while True:
            with Session(engine) as session:
                row = (
                    session.connection()
                    .execute(
                        text("""
                    SELECT advertiser_id,bucket,source_revision FROM read_bucket_state
                    WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
                      AND kind='creative_material' AND bucket BETWEEN :start AND :end
                      AND source_revision>derived_revision ORDER BY bucket DESC,advertiser_id
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                """),
                        {
                            "tenant": args.tenant,
                            "accounts": accounts,
                            "start": args.start,
                            "end": args.end,
                        },
                    )
                    .first()
                )
                if row is None:
                    break
                project_bucket(
                    session,
                    ReadBucketKey(
                        args.tenant, row.advertiser_id, "creative_material", row.bucket
                    ),
                    expected_version=row.source_revision,
                )
                session.commit()
                count += 1
        result = {"projected": count}
    else:
        with Session(engine) as session:
            result = (
                {
                    "enqueued": enqueue_backfill(
                        session,
                        tenant_id=args.tenant,
                        bc_id=args.bc,
                        start=args.start,
                        end=args.end,
                        now=datetime.now(UTC),
                        receipt=lambda value: print(  # noqa: T201 - committed batch receipt
                            json.dumps(value, sort_keys=True), flush=True
                        ),
                    )
                }
                if args.action == "enqueue"
                else audit(
                    session,
                    tenant_id=args.tenant,
                    bc_id=args.bc,
                    start=args.start,
                    end=args.end,
                )
            )
            session.commit()
    print(json.dumps(result, default=str, sort_keys=True))  # noqa: T201 - explicit CLI receipt


if __name__ == "__main__":
    main()
