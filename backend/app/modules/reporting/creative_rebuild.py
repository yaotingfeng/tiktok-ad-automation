"""分阶段上线的显式运维入口：本地身份回填、成对历史回补与只读对账。

不直接读写平台；回补进入现有冻结授权、限流和租约保护的报表队列。
"""

import argparse
import json
import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.planner import owned_accounts
from app.modules.reporting.store_models import AdSyncBc


def _plan(session, tenant_id, bc_id):
    plan = session.get(AdSyncBc, (tenant_id, bc_id))
    if plan is None or not plan.enabled:
        raise ValueError("active official BC sync plan required")
    return plan


def enqueue_backfill(session, *, tenant_id, bc_id, start, end, now):
    """只补当前归属账户已有总账日期；新建任务至多7日，近35天优先。

    已有覆盖任务仍由需求层复用，不改写其冻结范围；七日上限约束本轮新建任务。
    """
    if end < start:
        raise ValueError("invalid backfill date range")
    plan = _plan(session, tenant_id, bc_id)
    accounts = owned_accounts(session, plan)
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT advertiser_id,stat_date FROM ad_report_day
        WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[]))
          AND stat_date BETWEEN :start AND :end
        ORDER BY advertiser_id,stat_date
    """),
            {"tenant": tenant_id, "accounts": accounts, "start": start, "end": end},
        )
        .all()
    )
    windows = []
    for account in accounts:
        days = [row.stat_date for row in rows if row.advertiser_id == account]
        left = right = None
        for day in days:
            if left is not None and (
                day != right + timedelta(days=1) or (day - left).days >= 7
            ):
                windows.append((account, left, right))
                left = None
            if left is None:
                left = day
            right = day
        if left is not None:
            windows.append((account, left, right))
    count = 0
    for account, left, right in sorted(
        windows,
        key=lambda w: (w[2] < now.date() - timedelta(days=35), -w[2].toordinal(), w[0]),
    ):
        for kind in ("ad_day", "creative_day"):
            if enqueue_job(
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
            ):
                count += 1
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
