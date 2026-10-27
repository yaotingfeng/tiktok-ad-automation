"""关联需求按账户归并；运行中的读取保持冻结，新需求进入独立后继。"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlmodel import col, select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import ReportSyncJob

ACTIVE = ("QUEUED", "WAITING")
logger = logging.getLogger(__name__)

# 先收窄到现有部分索引覆盖的活动关联，再检查 JSON 游标；不能为找到少量旧
# 工作沿全历史排序扫描。物化边界也避免优化器把外层 JSON 谓词推回历史扫描。
RECONCILE_CANDIDATES_SQL = """
WITH active_links AS MATERIALIZED (
    SELECT id, created_at, progress ? 'coalesced_v1' AS coalesced
    FROM report_sync_job
    WHERE kind='material_links' AND status IN ('QUEUED','WAITING')
)
SELECT id FROM active_links WHERE NOT coalesced
ORDER BY created_at, id LIMIT :limit
"""

WAKE_CANDIDATES_SQL = """
WITH active_links AS MATERIALIZED (
    SELECT id, created_at, progress->>'dependency_probed_at' AS probed_at
    FROM report_sync_job
    WHERE kind='material_links' AND status IN ('QUEUED','WAITING')
      AND status='WAITING' AND error_code = ANY(:errors) AND due_at > :now
      AND claim_token IS NULL AND (claimed_until IS NULL OR claimed_until <= :now)
)
SELECT id FROM active_links
ORDER BY probed_at NULLS FIRST, created_at, id LIMIT :limit
"""


def _current(session, tenant_id, advertiser, start, end, now):
    account = session.get(AdvertiserAccount, (tenant_id, advertiser))
    return bool(
        account
        and account.timezone
        and start == end == now.astimezone(ZoneInfo(account.timezone)).date()
    )


def _scope(tenant_id, bc_id, actor_id, route, advertiser, campaigns):
    return hashlib.sha256(
        json.dumps(
            [
                str(tenant_id),
                bc_id,
                str(actor_id),
                route,
                advertiser,
                sorted(campaigns),
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _pending(
    session, *, tenant_id, bc_id, actor_id, frozen_route, advertiser, campaign_ids, now
):
    # SKIP LOCKED 避免事实发布等待正在执行网络读取的任务；有效租约也不可改变。
    return session.exec(
        select(ReportSyncJob)
        .where(
            ReportSyncJob.tenant_id == tenant_id,
            ReportSyncJob.bc_id == bc_id,
            ReportSyncJob.actor_id == actor_id,
            ReportSyncJob.frozen_route == frozen_route,
            ReportSyncJob.advertiser_ids == [advertiser],
            ReportSyncJob.campaign_ids == sorted(campaign_ids),
            ReportSyncJob.kind == "material_links",
            col(ReportSyncJob.status).in_(ACTIVE),
            (
                col(ReportSyncJob.claimed_until).is_(None)
                | (col(ReportSyncJob.claimed_until) <= now)
            ),
        )
        .order_by(col(ReportSyncJob.created_at), col(ReportSyncJob.id))
        .with_for_update(skip_locked=True)
    ).all()


def _extend(job, start, end):
    first, last = min(start, job.start_date), max(end, job.end_date)
    if (first, last) != (job.start_date, job.end_date):
        # 日期扩大后广告筛选可能改变；只丢弃查询游标，已经验证的映射仍在事实表中。
        job.progress = {
            "coalesced_v1": True,
            "initial_window": job.progress.get(
                "initial_window", [job.start_date.isoformat(), job.end_date.isoformat()]
            ),
        }
        job.start_date, job.end_date = first, last
    else:
        job.progress = job.progress | {"coalesced_v1": True}


def _merge(root, others, now):
    for row in others:
        _extend(root, row.start_date, row.end_date)
        root.priority = min(root.priority, row.priority)
        root.due_at = min(root.due_at, row.due_at)
        row.status, row.finished_at, row.error_code = (
            "CANCELLED",
            now,
            "report_work_coalesced",
        )
        row.claim_token = row.claimed_until = None
        row.progress = row.progress | {"merged_into": str(root.id)}
    _extend(root, root.start_date, root.end_date)


def enqueue_link_jobs(
    session,
    *,
    tenant_id,
    bc_id,
    actor_id,
    frozen_route,
    advertiser_groups,
    campaign_ids,
    start,
    end,
    priority,
    due_at=None,
    requested_at=None,
):
    now = datetime.now(UTC)
    created = []
    advertisers = sorted({a for group in advertiser_groups for a in group})
    for advertiser in advertisers:
        scope = _scope(
            tenant_id, bc_id, actor_id, frozen_route, advertiser, campaign_ids
        )
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"report-links:{scope}"},
        )
        current = _current(session, tenant_id, advertiser, start, end, now)
        candidates = [
            row
            for row in _pending(
                session,
                tenant_id=tenant_id,
                bc_id=bc_id,
                actor_id=actor_id,
                frozen_route=frozen_route,
                advertiser=advertiser,
                campaign_ids=campaign_ids,
                now=now,
            )
            if _current(
                session, tenant_id, advertiser, row.start_date, row.end_date, now
            )
            == current
        ]
        if candidates:
            root = candidates[0]
            _merge(root, candidates[1:], now)
            expanded = start < root.start_date or end > root.end_date
            _extend(root, start, end)
            root.priority = min(priority, root.priority)
            from app.modules.reporting.material_links import (
                DEPENDENCY_ERRORS,
                dependency_changed,
            )

            changed = root.error_code in DEPENDENCY_ERRORS and dependency_changed(
                session, root
            )
            if expanded or changed:
                root.due_at = min(root.due_at, due_at or now)
                root.error_code = None
                root.progress = root.progress | {"dependency_attempts": -1}
            session.flush()
            continue
        job_id = uuid4()
        # 键绑定本次工作实例：在途任务与后继可以同日期，但规划锁保证至多一个待领后继。
        session.add(
            ReportSyncJob(
                id=job_id,
                tenant_id=tenant_id,
                bc_id=bc_id,
                actor_id=actor_id,
                frozen_route=frozen_route,
                kind="material_links",
                priority=priority,
                advertiser_ids=[advertiser],
                campaign_ids=sorted(campaign_ids),
                start_date=start,
                end_date=end,
                due_at=due_at or now,
                job_key=f"links:{scope}:{job_id.hex}",
                progress={"coalesced_v1": True}
                | ({"requested_at": requested_at.isoformat()} if requested_at else {}),
            )
        )
        session.flush()
        created.append(job_id)
    return created


def reconcile_link_work(session, *, now, limit=50, deadline=None):
    """旧任务有界归并；原记录保留承接关系，不把归并计为数据已经补齐。"""
    session.flush()
    candidates = (
        session.connection()
        .execute(text(RECONCILE_CANDIDATES_SQL), {"limit": limit})
        .scalars()
        .all()
    )
    merged = 0
    for candidate_id in candidates:
        if deadline is not None and datetime.now(UTC) >= deadline:
            break
        candidate = session.get(ReportSyncJob, candidate_id)
        if candidate is None:
            continue
        if len(candidate.advertiser_ids) != 1:
            continue
        advertiser = candidate.advertiser_ids[0]
        scope = _scope(
            candidate.tenant_id,
            candidate.bc_id,
            candidate.actor_id,
            candidate.frozen_route,
            advertiser,
            candidate.campaign_ids,
        )
        if (
            not session.connection()
            .execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": f"report-links:{scope}"},
            )
            .scalar_one()
        ):
            continue
        rows = _pending(
            session,
            tenant_id=candidate.tenant_id,
            bc_id=candidate.bc_id,
            actor_id=candidate.actor_id,
            frozen_route=candidate.frozen_route,
            advertiser=advertiser,
            campaign_ids=candidate.campaign_ids,
            now=now,
        )
        for current in (True, False):
            group = [
                r
                for r in rows
                if _current(
                    session, r.tenant_id, advertiser, r.start_date, r.end_date, now
                )
                == current
            ]
            if group:
                _merge(group[0], group[1:], now)
                merged += len(group) - 1
        session.flush()
    return merged


def _orphaned_directory_sequence(session, job, *, now):
    """仅恢复当前缺口确实需要、又被错误淘汰且失去覆盖的定向目录请求。"""
    if job.error_code != "material_directory_pending" or len(job.advertiser_ids) != 1:
        return None
    waiting_ids = job.progress.get("waiting_ids", [])
    if not waiting_ids:
        return None
    from app.modules.ads.directory_supersession import directory_query_covers
    from app.modules.ads.sync_models import AdDirectoryRun
    from app.modules.reporting.store_models import MaterialPostLink

    done = aliased(MaterialPostLink)
    # 已解析帖子即使还留有另一条未解析使用行，也不能再被旧waiting_ids唤醒。
    resolved = (
        select(done.main_material_id)
        .where(
            done.tenant_id == MaterialPostLink.tenant_id,
            done.advertiser_id == MaterialPostLink.advertiser_id,
            done.main_material_id == MaterialPostLink.main_material_id,
            func.nullif(col(done.platform_material_id), "").is_not(None),
        )
        .exists()
    )
    needed = (
        select(MaterialPostLink.ad_remote_id)
        .where(
            MaterialPostLink.tenant_id == job.tenant_id,
            MaterialPostLink.advertiser_id == job.advertiser_ids[0],
            col(MaterialPostLink.main_material_id).in_(waiting_ids),
            ~resolved,
        )
        .distinct()
        .cte("needed_link_ads")
    )
    older, covering = AdDirectoryRun, aliased(AdDirectoryRun)
    # 覆盖只要求本任务仍缺失的广告，不拿旧请求里无关的其它ID扩大恢复范围。
    needed_ids = (
        select(func.jsonb_agg(needed.c.ad_remote_id))
        .where(col(older.query)["ids"].op("?")(needed.c.ad_remote_id))
        .correlate(older)
        .scalar_subquery()
    )
    scope = SimpleNamespace(
        **{
            key: getattr(older, key)
            for key in (
                "tenant_id",
                "advertiser_id",
                "bc_id",
                "actor_id",
                "frozen_route",
                "kind",
                "ad_type",
            )
        },
        query=func.jsonb_build_object(
            "ids",
            needed_ids,
            "parent_ids",
            col(older.query)["parent_ids"],
            "include_deleted",
            True,
        ),
    )
    covered = (
        select(covering.id)
        .where(
            directory_query_covers(scope, covering),
            or_(
                col(covering.status).in_(
                    ("QUEUED", "RUNNING", "ADMISSION_WAIT", "WAITING_REMOTE")
                ),
                and_(
                    col(covering.status) == "COMPLETE",
                    col(covering.coverage) == "COMPLETE",
                    col(covering.published_version).is_not(None),
                    col(covering.request_sequence) > older.request_sequence,
                ),
                # 与正常request_link_directory相同的十分钟去重期；暂时覆盖不记
                # 已处理，防止刚失败的请求被提前标记后再次陷入数小时退避。
                col(covering.created_at) >= now - timedelta(minutes=10),
            ),
        )
        .exists()
    )
    handled = job.progress.get("dependency_recovery_sequences", [])
    return session.exec(
        select(older.request_sequence)
        .where(
            older.tenant_id == job.tenant_id,
            older.advertiser_id == job.advertiser_ids[0],
            older.bc_id == job.bc_id,
            older.actor_id == job.actor_id,
            older.frozen_route == job.frozen_route,
            older.kind == "ad",
            older.status == "STALE",
            col(older.published_version).is_(None),
            col(older.error_code).in_(("superseded", "directory_run_stale")),
            col(older.query)["include_deleted"].astext == "true",
            func.jsonb_array_length(col(older.query)["ids"]) > 0,
            ~col(older.query).op("?")("material_child_v1"),
            needed_ids.is_not(None),
            col(older.request_sequence).not_in(handled),
            ~covered,
        )
        .order_by(col(older.request_sequence))
        .limit(1)
    ).first()


def wake_link_dependencies(session, *, now, limit=50, deadline=None):
    """有界轮转复核目录证据，历史任务也可被新目录唤醒，无需等待下一次事实刷新。"""
    from app.modules.reporting.material_links import (
        DEPENDENCY_ERRORS,
        dependency_changed,
    )

    session.flush()
    rows = (
        session.connection()
        .execute(
            text(WAKE_CANDIDATES_SQL),
            {"errors": sorted(DEPENDENCY_ERRORS), "now": now, "limit": limit},
        )
        .scalars()
        .all()
    )
    count = 0
    for job_id in rows:
        if deadline is not None and datetime.now(UTC) >= deadline:
            break
        # 候选只取ID；逐条锁定并重验当前状态，不能预锁整批阻挡正常领取。
        job = session.exec(
            select(ReportSyncJob)
            .where(
                ReportSyncJob.id == job_id,
                ReportSyncJob.kind == "material_links",
                ReportSyncJob.status == "WAITING",
                col(ReportSyncJob.error_code).in_(DEPENDENCY_ERRORS),
                ReportSyncJob.due_at > now,
                col(ReportSyncJob.claim_token).is_(None),
                col(ReportSyncJob.claimed_until).is_(None)
                | (col(ReportSyncJob.claimed_until) <= now),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if job is None:
            continue
        remaining = (
            1.0 if deadline is None else (deadline - datetime.now(UTC)).total_seconds()
        )
        if remaining <= 0:
            break
        changed = False
        recovery_sequence = None
        try:
            # 冷读可能超过一秒；只取消本条提前复核。原任务到期读取继续兜底，
            # 不假定证据已变化，也不因一个慢账户回滚此前成功的唤醒。
            with (
                session.begin_nested(),
                query_budget(session, seconds=min(1.0, remaining)),
            ):
                recovery_sequence = _orphaned_directory_sequence(session, job, now=now)
                if recovery_sequence is None:
                    changed = dependency_changed(session, job)
        except (DBAPIError, QueryBudgetExceeded) as error:
            if (
                isinstance(error, DBAPIError)
                and getattr(error.orig, "sqlstate", None) != "57014"
            ):
                raise
            changed, recovery_sequence = False, None
            logger.info("report link dependency probe deferred: job=%s", job_id)
        if recovery_sequence is not None:
            # 只提前原任务；其正常执行重新校验授权/付费缺口并经唯一入口建目录。
            # 记录实际触发集合而非最大序号：较早的暂时覆盖失败后仍能被恢复。
            job.due_at = now
            job.progress = job.progress | {
                "dependency_recovery_sequences": sorted(
                    {
                        *job.progress.get("dependency_recovery_sequences", []),
                        recovery_sequence,
                    }
                )
            }
            count += 1
        elif changed:
            job.due_at, job.error_code = now, None
            job.progress = job.progress | {"dependency_attempts": -1}
            count += 1
        # 超时也持久记录轮转位置；只能表示尝试过，不能更新真实证据签名。
        job.progress = job.progress | {"dependency_probed_at": now.isoformat()}
        session.flush()
    return count
