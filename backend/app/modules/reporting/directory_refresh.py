"""热报发现的在投对象优先补目录，避免全量历史队列遮蔽自动规则。"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.automation_rules.models import AutomationRule
from app.modules.reporting.lanes import report_workload
from app.modules.reporting.store_models import ReportSyncJob

# 每份完整热报最多补 100 条广告；查询仍由现有目录服务按 100 ID 分片。
MAX_REFRESH_ADS = 100
DIRECTORY_FRESHNESS = timedelta(minutes=2)
GENERAL_DIRECTORY_FRESHNESS = timedelta(minutes=30)
# 30分钟是新鲜度上限，25分钟即安排刷新，预留热报周期、排队与接口响应时间。
GENERAL_DIRECTORY_REFRESH_INTERVAL = timedelta(minutes=25)
_AD_TYPES = {
    "UPGRADED_SMART_PLUS": "SMART_PLUS",
    "SMART_PLUS": "LEGACY_SMART_PLUS",
    "MANUAL": "REGULAR",
}
_ACTIVE = ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")


def rule_accounts(session, *, tenant_id, bc_id, accounts):
    """规则覆盖的账户优先刷新状态；普通目录降低重复读取，给财务事实留下配额。"""
    covered = set()
    rules = session.exec(
        select(AutomationRule).where(
            AutomationRule.tenant_id == tenant_id, col(AutomationRule.enabled).is_(True)
        )
    ).all()
    for rule in rules:
        scope = rule.definition.get("accounts", {})
        if scope.get("mode") == "ALL" or (
            scope.get("mode") == "BC" and bc_id in scope.get("bc_ids", ())
        ):
            return set(accounts)
        if scope.get("mode") == "ACCOUNTS":
            covered.update(set(scope.get("advertiser_ids", ())) & set(accounts))
    return covered


def refresh_report_directory(
    session: Session, job: ReportSyncJob, *, now: datetime | None = None
) -> int:
    """仅补本次已发布热报中当地今天有消耗的广告及父链，不借此搬运全量目录。"""
    if not settings.ADS_SYNC_ENABLED or job.kind != "ad_day" or job.priority != 1:
        return 0
    now = now or datetime.now(UTC)
    strict_accounts = rule_accounts(
        session, tenant_id=job.tenant_id, bc_id=job.bc_id, accounts=job.advertiser_ids
    )
    rows = list(
        session.connection()
        .execute(
            text("""
        SELECT r.advertiser_id, r.ad_id, r.adgroup_id, r.campaign_id, r.campaign_automation_type,
          a.observed_at AS ad_observed_at, g.observed_at AS adgroup_observed_at,
          c.observed_at AS campaign_observed_at
        FROM ad_report_day r
        LEFT JOIN ad_object a ON a.tenant_id=r.tenant_id AND a.advertiser_id=r.advertiser_id AND a.kind='ad' AND a.remote_id=r.ad_id
        LEFT JOIN ad_object g ON g.tenant_id=r.tenant_id AND g.advertiser_id=r.advertiser_id AND g.kind='adgroup' AND g.remote_id=r.adgroup_id
        LEFT JOIN ad_object c ON c.tenant_id=r.tenant_id AND c.advertiser_id=r.advertiser_id AND c.kind='campaign' AND c.remote_id=r.campaign_id
        WHERE r.tenant_id=:tenant AND r.sync_job_id=:job AND r.advertiser_id=ANY(:accounts) AND r.spend>0
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND EXISTS (
            SELECT 1 FROM (VALUES ('ad',r.ad_id,a.observed_at),
              ('adgroup',r.adgroup_id,g.observed_at),
              ('campaign',r.campaign_id,c.observed_at)) target(kind,remote_id,observed_at)
            WHERE (target.observed_at IS NULL OR target.observed_at < CASE
              WHEN r.advertiser_id=ANY(:strict_accounts) THEN :cutoff ELSE :general_cutoff END)
            AND NOT EXISTS (
            SELECT 1 FROM ad_directory_run d WHERE d.tenant_id=r.tenant_id
              AND d.bc_id=:bc AND d.actor_id=:actor AND d.frozen_route=CAST(:route AS jsonb)
              AND d.advertiser_id=r.advertiser_id AND d.kind=target.kind
              AND d.ad_type=CASE r.campaign_automation_type WHEN 'UPGRADED_SMART_PLUS' THEN 'SMART_PLUS'
                WHEN 'SMART_PLUS' THEN 'LEGACY_SMART_PLUS' WHEN 'MANUAL' THEN 'REGULAR' END
              AND d.workload='live'
              AND d.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE')
              AND d.query->'ids' ? target.remote_id))
        ORDER BY (a.remote_id IS NOT NULL AND g.remote_id IS NOT NULL AND c.remote_id IS NOT NULL),
          LEAST(a.observed_at,g.observed_at,c.observed_at) ASC NULLS FIRST,
          r.spend DESC, r.advertiser_id, r.ad_id LIMIT :limit
    """),
            {
                "tenant": job.tenant_id,
                "job": job.id,
                "bc": job.bc_id,
                "actor": job.actor_id,
                "route": json.dumps(job.frozen_route),
                "accounts": job.advertiser_ids,
                "now": now,
                "cutoff": now - DIRECTORY_FRESHNESS,
                "general_cutoff": now - GENERAL_DIRECTORY_REFRESH_INTERVAL,
                "strict_accounts": sorted(strict_accounts),
                "limit": MAX_REFRESH_ADS,
            },
        )
        .mappings()
    )
    if not rows:
        return 0
    # 同一冻结 BC 的并发热报合并未完成回读；用户写后回读仍每次生成新请求。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"report-directory:{job.tenant_id}:{job.bc_id}"},
    )
    pending = session.exec(
        select(AdDirectoryRun).where(
            AdDirectoryRun.tenant_id == job.tenant_id,
            AdDirectoryRun.bc_id == job.bc_id,
            AdDirectoryRun.actor_id == job.actor_id,
            AdDirectoryRun.frozen_route == job.frozen_route,
            AdDirectoryRun.workload == "live",
            col(AdDirectoryRun.advertiser_id).in_(job.advertiser_ids),
            col(AdDirectoryRun.status).in_(_ACTIVE),
        )
    ).all()
    covered = {
        (run.advertiser_id, run.ad_type, run.kind, remote_id)
        for run in pending
        for remote_id in run.query.get("ids", ())
    }
    refs = {}
    types = defaultdict(lambda: defaultdict(set))
    for row in rows:
        ad_type = _AD_TYPES.get(row.campaign_automation_type)
        if ad_type is None:
            continue  # 报表未证明类型时不猜测，也不把金额当目录状态。
        cutoff = now - (
            DIRECTORY_FRESHNESS
            if row.advertiser_id in strict_accounts
            else GENERAL_DIRECTORY_REFRESH_INTERVAL
        )
        for kind, column in (
            ("campaign", "campaign_id"),
            ("adgroup", "adgroup_id"),
            ("ad", "ad_id"),
        ):
            # 三层分别判断：父链某层过期，不应连带重读仍新鲜的另外两层。
            observed_at = row[f"{kind}_observed_at"]
            if observed_at is not None and observed_at >= cutoff:
                continue
            key = (row.advertiser_id, ad_type, kind, row[column])
            if key not in covered:
                refs[key] = EntityRef(
                    job.tenant_id, row.advertiser_id, kind, row[column]
                )
                types[row.advertiser_id][ad_type].add(kind)
    if not refs:
        return 0
    created = 0
    # 目录服务的 refs 不带广告类型；分类型请求，避免混合账户把其它类型的 ID 发错端点。
    for ad_type in sorted({key[1] for key in refs}):
        group = {key: ref for key, ref in refs.items() if key[1] == ad_type}
        advertisers = sorted({key[0] for key in group})
        created += request_directory(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            frozen_route=job.frozen_route,
            actor_id=job.actor_id,
            advertiser_ids=advertisers,
            scope="targeted",
            refs=tuple(group.values()),
            ad_types={adv: {ad_type: types[adv][ad_type]} for adv in advertisers},
        )
    return created


def request_link_directory(
    session: Session, job: ReportSyncJob, *, now: datetime
) -> int:
    """已确认广告但缺视频时补读含删除对象的目录，不依赖本地广告本体先存在。"""
    if not settings.ADS_SYNC_ENABLED:
        return 0
    workload = report_workload(session, job, now=now)
    rows = (
        session.connection()
        .execute(
            text("""
        SELECT DISTINCT l.ad_remote_id, COALESCE(a.ad_type, 'SMART_PLUS') AS ad_type
        FROM material_report_day d
        JOIN material_post_link l ON l.tenant_id=d.tenant_id AND l.advertiser_id=d.advertiser_id
          AND l.main_material_id=d.main_material_id
        LEFT JOIN ad_object a ON a.tenant_id=l.tenant_id AND a.advertiser_id=l.advertiser_id
          AND a.kind='ad' AND a.remote_id=l.ad_remote_id
        WHERE d.tenant_id=:t AND d.advertiser_id=:adv AND d.stat_date BETWEEN :start AND :end
          AND NULLIF(l.platform_material_id, '') IS NULL
          AND (a.ad_type IS NULL OR a.ad_type IN ('SMART_PLUS','LEGACY_SMART_PLUS','REGULAR'))
          AND NOT EXISTS (SELECT 1 FROM material_post_link done
            WHERE done.tenant_id=d.tenant_id AND done.advertiser_id=d.advertiser_id
              AND done.main_material_id=d.main_material_id AND NULLIF(done.platform_material_id, '') IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM ad_directory_run r
            WHERE r.tenant_id=:t AND r.bc_id=:bc AND r.actor_id=:actor
              AND r.frozen_route=CAST(:route AS jsonb) AND r.advertiser_id=:adv
              AND (:workload='background' OR r.workload='live')
              AND r.kind='ad' AND r.ad_type=COALESCE(a.ad_type, 'SMART_PLUS')
              AND r.query->'ids' ? l.ad_remote_id AND r.query->>'include_deleted'='true'
              AND (r.status IN ('QUEUED','RUNNING','ADMISSION_WAIT','WAITING_REMOTE') OR r.created_at>=:cutoff))
        ORDER BY l.ad_remote_id, ad_type LIMIT :limit
    """),
            {
                "workload": workload,
                "t": job.tenant_id,
                "adv": job.advertiser_ids[0],
                "start": job.start_date,
                "end": job.end_date,
                "bc": job.bc_id,
                "actor": job.actor_id,
                "route": json.dumps(job.frozen_route),
                "cutoff": now - timedelta(minutes=10),
                "limit": MAX_REFRESH_ADS,
            },
        )
        .all()
    )
    count = 0
    # material_post_link 的唯一生产写入源是升级 Smart+ 素材概览/拆分报表，
    # 其中 smart_plus_ad_id 已证明类型；缺本地 AdObject 时仍可按该证据回读。
    # 历史报表保留已删除广告的消耗，普通定向刷新默认排除删除对象，不能用于补历史身份。
    for ad_type in sorted({r.ad_type for r in rows}):
        count += request_directory(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            actor_id=job.actor_id,
            frozen_route=job.frozen_route,
            advertiser_ids=job.advertiser_ids,
            scope="targeted",
            include_deleted=True,
            workload=workload,
            refs=tuple(
                EntityRef(job.tenant_id, job.advertiser_ids[0], "ad", r.ad_remote_id)
                for r in rows
                if r.ad_type == ad_type
            ),
            ad_types={job.advertiser_ids[0]: {ad_type: {"ad"}}},
        )
    return count
