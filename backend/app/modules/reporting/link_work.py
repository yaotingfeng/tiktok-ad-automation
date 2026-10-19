"""关联需求按账户归并；运行中的读取保持冻结，新需求进入独立后继。"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import col, select

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.store_models import ReportSyncJob

ACTIVE = ("QUEUED", "WAITING")


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
    candidates = session.exec(
        select(ReportSyncJob)
        .where(
            ReportSyncJob.kind == "material_links",
            col(ReportSyncJob.status).in_(ACTIVE),
            text("NOT (report_sync_job.progress ? 'coalesced_v1')"),
        )
        .order_by(col(ReportSyncJob.created_at), col(ReportSyncJob.id))
        .limit(limit)
    ).all()
    merged = 0
    for candidate in candidates:
        if deadline is not None and datetime.now(UTC) >= deadline:
            break
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


def wake_link_dependencies(session, *, now, limit=50, deadline=None):
    """有界轮转复核目录证据，历史任务也可被新目录唤醒，无需等待下一次事实刷新。"""
    from app.modules.reporting.material_links import (
        DEPENDENCY_ERRORS,
        dependency_changed,
    )

    rows = session.exec(
        select(ReportSyncJob)
        .where(
            ReportSyncJob.kind == "material_links",
            ReportSyncJob.status == "WAITING",
            col(ReportSyncJob.error_code).in_(DEPENDENCY_ERRORS),
            ReportSyncJob.due_at > now,
            col(ReportSyncJob.claim_token).is_(None),
        )
        .order_by(
            text("progress->>'dependency_probed_at' NULLS FIRST"),
            col(ReportSyncJob.created_at),
            col(ReportSyncJob.id),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for job in rows:
        if deadline is not None and datetime.now(UTC) >= deadline:
            break
        if dependency_changed(session, job):
            job.due_at, job.error_code = now, None
            job.progress = job.progress | {"dependency_attempts": -1}
            count += 1
        job.progress = job.progress | {"dependency_probed_at": now.isoformat()}
    session.flush()
    return count
