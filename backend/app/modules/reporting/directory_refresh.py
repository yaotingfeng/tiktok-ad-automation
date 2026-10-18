"""热报发现的在投对象优先补目录，避免全量历史队列遮蔽自动规则。"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting.store_models import ReportSyncJob

# 每份完整热报最多补 100 条广告；查询仍由现有目录服务按 100 ID 分片。
MAX_REFRESH_ADS = 100
DIRECTORY_FRESHNESS = timedelta(hours=1)
_AD_TYPES = {
    "UPGRADED_SMART_PLUS": "SMART_PLUS",
    "SMART_PLUS": "LEGACY_SMART_PLUS",
    "MANUAL": "REGULAR",
}
_ACTIVE = ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")


def refresh_report_directory(
    session: Session, job: ReportSyncJob, *, now: datetime | None = None
) -> int:
    """仅补本次已发布热报中当地今天有消耗的广告及父链，不借此搬运全量目录。"""
    if not settings.ADS_SYNC_ENABLED or job.kind != "ad_day" or job.priority != 1:
        return 0
    now = now or datetime.now(UTC)
    rows = list(
        session.connection()
        .execute(
            text("""
        SELECT r.advertiser_id, r.ad_id, r.adgroup_id, r.campaign_id, r.campaign_automation_type
        FROM ad_report_day r
        LEFT JOIN ad_object a ON a.tenant_id=r.tenant_id AND a.advertiser_id=r.advertiser_id AND a.kind='ad' AND a.remote_id=r.ad_id
        LEFT JOIN ad_object g ON g.tenant_id=r.tenant_id AND g.advertiser_id=r.advertiser_id AND g.kind='adgroup' AND g.remote_id=r.adgroup_id
        LEFT JOIN ad_object c ON c.tenant_id=r.tenant_id AND c.advertiser_id=r.advertiser_id AND c.kind='campaign' AND c.remote_id=r.campaign_id
        WHERE r.tenant_id=:tenant AND r.sync_job_id=:job AND r.advertiser_id=ANY(:accounts) AND r.spend>0
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
          AND (a.remote_id IS NULL OR a.observed_at<:cutoff OR g.remote_id IS NULL OR g.observed_at<:cutoff OR c.remote_id IS NULL OR c.observed_at<:cutoff)
        ORDER BY r.spend DESC, r.advertiser_id, r.ad_id LIMIT :limit
    """),
            {
                "tenant": job.tenant_id,
                "job": job.id,
                "accounts": job.advertiser_ids,
                "now": now,
                "cutoff": now - DIRECTORY_FRESHNESS,
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
        for kind, column in (
            ("campaign", "campaign_id"),
            ("adgroup", "adgroup_id"),
            ("ad", "ad_id"),
        ):
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
