"""周期完整目录只是低频校准，实时对象由报表发现和操作回读维护。"""

import json
from datetime import timedelta

from sqlalchemy import text
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.ads.directory_dispatch import directory_step_key
from app.modules.ads.sync_models import AdDirectoryPage

PERIODIC_KEY = "periodic_directory_v1"
ACTIVE_INTERVAL = timedelta(days=1)
INACTIVE_INTERVAL = timedelta(days=7)
ACTIVITY_MAX_AGE = timedelta(minutes=30)


def unstarted_without_delivery(session, run):
    """调用方持有运行锁，页和本代消息均不存在时才允许调整调度。"""
    if (
        run.status != "QUEUED"
        or run.next_page != 1
        or run.claim_token is not None
        or run.claimed_until is not None
        or run.published_version is not None
    ):
        return False
    if (
        session.exec(
            select(AdDirectoryPage.page)
            .where(AdDirectoryPage.run_id == run.id)
            .limit(1)
        ).first()
        is not None
    ):
        return False
    return (
        session.exec(
            select(PendingDispatch.id)
            .where(
                PendingDispatch.tenant_id == run.tenant_id,
                PendingDispatch.task_key == directory_step_key(run),
            )
            .limit(1)
        ).first()
        is None
    )


def calibration_interval(plan, advertiser_id, now):
    # 未完成或过期的筛查不证明账户冷却，保守按每日巡检；不额外扩大报表范围。
    cold = (
        plan.active_screened_at is not None
        and now - ACTIVITY_MAX_AGE <= plan.active_screened_at <= now
        and advertiser_id not in plan.last_active_advertiser_ids
    )
    return INACTIVE_INTERVAL if cold else ACTIVE_INTERVAL


def recent_calibration(
    session,
    *,
    tenant_id,
    advertiser_id,
    bc_id,
    actor_id,
    frozen_route,
    partition_key,
    cutoff,
):
    # 严格使用同一查询分区、冻结授权的已发布成功，定向/其他层级不能充当全量。
    # 周期全量请求必含已删除对象；显式保留此谓词，近期成功部分索引才可
    # 按账户与截止时间先排除旧历史，再核对精确冻结分区，避免历史扫描后排序。
    return (
        session.connection()
        .execute(
            text("""
        SELECT completed_at FROM ad_directory_run
        WHERE tenant_id=:tenant AND advertiser_id=:advertiser
          AND partition_key=:partition AND bc_id=:bc AND actor_id=:actor
          AND frozen_route=CAST(:route AS jsonb)
          AND status='COMPLETE' AND published_version IS NOT NULL
          AND CAST(query->>'include_deleted' AS boolean) IS TRUE
          AND completed_at>:cutoff
        ORDER BY request_sequence DESC LIMIT 1
    """),
            {
                "tenant": tenant_id,
                "advertiser": advertiser_id,
                "partition": partition_key,
                "bc": bc_id,
                "actor": actor_id,
                "route": json.dumps(frozen_route),
                "cutoff": cutoff,
            },
        )
        .scalar_one_or_none()
    )
