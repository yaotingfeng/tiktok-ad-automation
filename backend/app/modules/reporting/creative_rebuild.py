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
    """只补当前归属账户已有总账日期；每段至多7日，近35天优先。"""
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
            ):
                count += 1
    return count


def audit(session, *, tenant_id, bc_id, start, end):
    """独立列出事实、身份及差额；不把未关联金额算成缺失金额。"""
    plan = _plan(session, tenant_id, bc_id)
    params = {
        "tenant": tenant_id,
        "accounts": owned_accounts(session, plan),
        "start": start,
        "end": end,
    }
    row = (
        session.connection()
        .execute(
            text("""
        WITH a AS (SELECT count(*) AS rows,count(DISTINCT (advertiser_id,stat_date)) AS buckets,
          sum(spend) AS spend,sum(revenue_total) AS revenue FROM ad_report_day
          WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[])) AND stat_date BETWEEN :start AND :end),
        c AS (SELECT count(*) AS rows,count(DISTINCT (advertiser_id,stat_date)) AS buckets,
          sum(spend) AS spend,sum(revenue_total) AS revenue FROM creative_report_day
          WHERE tenant_id=:tenant AND advertiser_id=ANY(CAST(:accounts AS text[])) AND stat_date BETWEEN :start AND :end),
        p AS (SELECT count(*) FILTER(WHERE attribution_status='UNMAPPED') AS unmapped_rows,
          sum(spend) FILTER(WHERE attribution_status='UNMAPPED') AS unmapped_spend,
          sum(revenue_total) FILTER(WHERE attribution_status='UNMAPPED') AS unmapped_revenue,
          count(*) FILTER(WHERE attribution_status='RESIDUAL') AS residual_rows,
          sum(spend) FILTER(WHERE attribution_status='RESIDUAL') AS residual_spend,
          sum(revenue_total) FILTER(WHERE attribution_status='RESIDUAL') AS residual_revenue,
          count(*) FILTER(WHERE source_status='STALE') AS stale_rows
          FROM creative_material_entity_day WHERE tenant_id=:tenant
          AND advertiser_id=ANY(CAST(:accounts AS text[])) AND stat_date BETWEEN :start AND :end),
        b AS (SELECT count(*) AS dirty FROM read_bucket_state WHERE tenant_id=:tenant
          AND advertiser_id=ANY(CAST(:accounts AS text[])) AND kind='creative_material'
          AND bucket BETWEEN :start AND :end AND source_revision>derived_revision)
        SELECT a.rows AS ad_rows,a.buckets AS ad_buckets,a.spend AS ad_spend,a.revenue AS ad_revenue,
          c.rows AS creative_rows,c.buckets AS creative_buckets,c.spend AS creative_spend,c.revenue AS creative_revenue,p.*,b.dirty
        FROM a,c,p,b
    """),
            params,
        )
        .mappings()
        .one()
    )
    return dict(row)


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
