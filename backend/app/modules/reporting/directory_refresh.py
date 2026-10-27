"""热报发现的在投对象优先补目录，避免全量历史队列遮蔽自动规则。"""

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.contracts.ads import EntityKind, EntityRef
from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.automation_rules.models import AutomationRule
from app.modules.reporting.lanes import report_workload
from app.modules.reporting.material_value import (
    MATERIAL_WINDOW_VALUES_SQL,
    material_value_parameters,
    valuable_material_condition,
)
from app.modules.reporting.store_models import ReportSyncJob

# 素材关联补读保持原有单轮上限；广告日报刷新不截断账户对象集合。
MAX_REFRESH_ADS = 100
STATUS_INTERVAL = timedelta(minutes=30)
PARENT_INTERVAL = timedelta(minutes=60)
_AD_TYPES = {
    "UPGRADED_SMART_PLUS": "SMART_PLUS",
    "SMART_PLUS": "LEGACY_SMART_PLUS",
    "MANUAL": "REGULAR",
}
_ACTIVE = ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")


def rule_accounts(
    session: Session, *, tenant_id: UUID, bc_id: str, accounts: Sequence[str]
) -> set[str]:
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


def directory_interval(kind: str, *, has_rules: bool) -> timedelta:
    """规则账户三层状态必须跟上同一实时轮次，普通账户上级配置可更慢。"""
    # planner 经 jobs 导入本模块；在运行时取常量，避免初始化循环，也避免复制周期值。
    from app.modules.reporting import planner

    if has_rules:
        return planner.HOT_INTERVAL
    return STATUS_INTERVAL if kind == "ad" else PARENT_INTERVAL


def refresh_report_directory(
    session: Session, job: ReportSyncJob, *, now: datetime | None = None
) -> int:
    """只选本任务账户当地今日有消耗对象，按账户、类型、层级对齐刷新轮次。"""
    if not settings.ADS_SYNC_ENABLED or job.kind != "ad_day" or job.priority != 1:
        return 0
    from app.modules.reporting.planner import REFRESH_MARGIN

    now = now or datetime.now(UTC)
    # 先取得规划锁再读取目录年龄和在途运行，保证并发发布看到前一事务的完整计划。
    # 只约束热报规划；用户操作后的回读仍走既有独立路径。
    session.connection().execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"report-directory:{job.tenant_id}:{job.bc_id}"},
    )
    strict_accounts = rule_accounts(
        session, tenant_id=job.tenant_id, bc_id=job.bc_id, accounts=job.advertiser_ids
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
    # 同账户同类型同层任意 live 运行在途即让出本轮，不能因 ID 不同再制造碎片。
    covered = {(run.advertiser_id, run.ad_type, run.kind) for run in pending}
    rows = (
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
        WHERE r.tenant_id=:tenant AND r.advertiser_id=ANY(:accounts) AND r.spend>0
          AND r.stat_date BETWEEN :first_day AND :last_day
          AND r.stat_date=(CAST(:now AS timestamptz) AT TIME ZONE r.timezone)::date
    """),
            {
                "tenant": job.tenant_id,
                "accounts": job.advertiser_ids,
                "now": now,
                "first_day": now.astimezone(UTC).date() - timedelta(days=1),
                "last_day": now.astimezone(UTC).date() + timedelta(days=1),
            },
        )
        .mappings()
    )
    # 不限 sync_job_id：同账户今日事实可由不同任务发布，必须整批对齐所有有消耗对象。
    # 日期粗范围用于分区裁剪，精确当地日条件仍以事实时区为准。
    objects: dict[tuple[str, str, EntityKind], dict[str, datetime | None]] = (
        defaultdict(dict)
    )
    layers: tuple[tuple[EntityKind, str], ...] = (
        ("campaign", "campaign_id"),
        ("adgroup", "adgroup_id"),
        ("ad", "ad_id"),
    )
    for row in rows:
        ad_type = _AD_TYPES.get(row.campaign_automation_type)
        if ad_type is None:
            continue  # 类型未获报表证明时不猜测平台端点。
        for kind, column in layers:
            key = (row.advertiser_id, ad_type, kind)
            if row[column]:
                objects[key][row[column]] = row[f"{kind}_observed_at"]
    expired_layers: set[tuple[str, EntityKind]] = set()
    for (advertiser, _ad_type, kind), observations in objects.items():
        cutoff = now - (
            directory_interval(kind, has_rules=advertiser in strict_accounts)
            - REFRESH_MARGIN
        )
        if any(at is not None and at <= cutoff for at in observations.values()):
            expired_layers.add((advertiser, kind))
    refs_by_type: dict[str, list[EntityRef]] = defaultdict(list)
    types: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for (advertiser, ad_type, kind), observations in sorted(objects.items()):
        if (advertiser, ad_type, kind) in covered:
            continue
        # 到期按账户/层级合并，跨广告类型一起对齐；在途约束仍按类型分别让出。
        # 仅缺失时只补缺失，避免新投放一个对象连带重读整个账户。
        expired = (advertiser, kind) in expired_layers
        for remote_id, observed_at in sorted(observations.items()):
            if expired or observed_at is None:
                refs_by_type[ad_type].append(
                    EntityRef(job.tenant_id, advertiser, kind, remote_id)
                )
                types[advertiser][ad_type].add(kind)
    created = 0
    # 沿用唯一目录请求入口的100 ID分片；分类型传 refs，避免混合类型误发端点。
    for ad_type, refs in sorted(refs_by_type.items()):
        advertisers = sorted({ref.advertiser_id for ref in refs})
        created += request_directory(
            session,
            tenant_id=job.tenant_id,
            bc_id=job.bc_id,
            frozen_route=job.frozen_route,
            actor_id=job.actor_id,
            advertiser_ids=advertisers,
            scope="targeted",
            dispatch_immediately=False,
            refs=tuple(refs),
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
            text(f"""
        WITH material_values AS ({MATERIAL_WINDOW_VALUES_SQL})
        SELECT DISTINCT l.ad_remote_id, COALESCE(a.ad_type, 'SMART_PLUS') AS ad_type
        FROM material_report_day d
        JOIN material_post_link l ON l.tenant_id=d.tenant_id AND l.advertiser_id=d.advertiser_id
          AND l.main_material_id=d.main_material_id
        LEFT JOIN ad_object a ON a.tenant_id=l.tenant_id AND a.advertiser_id=l.advertiser_id
          AND a.kind='ad' AND a.remote_id=l.ad_remote_id
        WHERE d.tenant_id=:t AND d.advertiser_id=:adv AND d.stat_date BETWEEN :start AND :end
          AND {valuable_material_condition("d")}
          AND NULLIF(l.platform_material_id, '') IS NULL
          AND (a.ad_type IS NULL OR a.ad_type IN ('SMART_PLUS','LEGACY_SMART_PLUS','REGULAR'))
          -- Spark 缺精确桥接且已有完整目录时，重读同一目录不能恢复旧帖身份。
          -- 缺本体/完整素材、已有桥接、非 Spark 仍保留原补读路径。
          AND NOT (l.main_material_type='VIDEO_SPARK_ADS'
            AND NULLIF(l.ad_material_id, '') IS NULL AND a.remote_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM ad_material_reference u
              WHERE u.tenant_id=l.tenant_id AND u.advertiser_id=l.advertiser_id
                AND u.ad_remote_id=l.ad_remote_id AND u.complete))
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
                **material_value_parameters(
                    job.tenant_id,
                    [(job.advertiser_ids[0], job.start_date, job.end_date)],
                ),
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
            dispatch_immediately=False,
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
