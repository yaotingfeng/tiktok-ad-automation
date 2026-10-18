"""定向回读与批量目录共用领取合同，但不共用批量读取的排队位置。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text
from sqlmodel import col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task

TARGETED_TASK_NAME = "ads.targeted_step"
TASK_NAME = "ads.sync_step"
PUBLISH_TASK_NAME = "ads.publish_step"
register_dispatch_task(TASK_NAME, "ads-directory")
register_dispatch_task(PUBLISH_TASK_NAME, "ads-publish")
register_dispatch_task(TARGETED_TASK_NAME, "ads-live-directory")
register_dispatch_task("reporting.plan_due", "control")
# 定向回读（在投广告及父链的状态刷新）同时在途上限。单步约 1 秒，实时目录 Worker 10 进程；
# 窗口 8 实测每 10 分钟仅完成约 230 个而新建约 330 个，排队超过 2 小时，目录 30 分钟新鲜度无法达成。
# 放到 32 让 Worker 持续有活；对 TikTok 的实际调用仍由调用准入策略限流。
TARGETED_DISPATCH_WINDOW = 32


def targeted_capacity(session, now=None) -> int:
    """生产者和恢复扫描共用同一事务锁；未领取的 ID 请求留在数据库。"""
    from app.jobs.models import PendingDispatch
    from app.modules.ads.sync_models import AdDirectoryRun

    now = now or datetime.now(UTC)
    locked = (
        session.connection()
        .execute(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('tkada:directory-dispatch-window', 0))"
            )
        )
        .scalar_one()
    )
    if not locked:
        return 0
    key = func.concat(
        TARGETED_TASK_NAME + ":",
        col(AdDirectoryRun.id),
        ":",
        col(AdDirectoryRun.claim_generation),
        ":page:",
        col(AdDirectoryRun.next_page),
    )
    enqueued = (
        select(PendingDispatch.id)
        .where(
            PendingDispatch.tenant_id == AdDirectoryRun.tenant_id,
            PendingDispatch.task_key == key,
            col(PendingDispatch.published_at).is_(None)
            | (col(PendingDispatch.published_at) > now - timedelta(minutes=2)),
        )
        .exists()
    )
    targeted = (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["ids"]), 0) > 0
    ) | (
        func.coalesce(func.jsonb_array_length(AdDirectoryRun.query["parent_ids"]), 0)
        > 0
    )
    inflight = session.exec(
        select(func.count())
        .select_from(AdDirectoryRun)
        .where(
            targeted,
            col(AdDirectoryRun.status).in_(["QUEUED", "RUNNING", "WAITING_REMOTE"]),
            enqueued | (col(AdDirectoryRun.claimed_until) > now),
        )
    ).one()
    return max(0, TARGETED_DISPATCH_WINDOW - inflight)


def is_targeted(run) -> bool:
    return bool(run.query.get("ids") or run.query.get("parent_ids"))


def directory_step_key(run) -> str:
    prefix = TARGETED_TASK_NAME if is_targeted(run) else TASK_NAME
    return f"{prefix}:{run.id}:{run.claim_generation}:page:{run.next_page}"


def enqueue_directory_step(
    session, run, *, publish_only=False, initial=False, available_at=None
) -> None:
    if initial and is_targeted(run) and not targeted_capacity(session):
        return
    dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TARGETED_TASK_NAME
        if is_targeted(run)
        else (PUBLISH_TASK_NAME if publish_only else TASK_NAME),
        task_key=directory_step_key(run),
        payload={"run_id": str(run.id), "claim_generation": run.claim_generation},
    )
    if available_at is not None:
        from app.jobs.models import PendingDispatch

        dispatch = session.get(PendingDispatch, dispatch_id)
        # 等待额度的续步仍走原 outbox；到期前不能发布，避免忙循环和空占租约。
        dispatch.available_at = available_at
        session.add(dispatch)


def enqueue_directory_refill(session, run) -> None:
    """首次发布同事务唤醒有界派发，空槽无需再等下一个三十秒扫描。"""
    if not settings.ADS_SYNC_ENABLED:
        return
    enqueue_after_commit(
        session,
        context=TenantContext(run.tenant_id, run.actor_id, "operator"),
        task_name="reporting.plan_due",
        task_key=f"directory-refill:{run.id}:{run.claim_generation}",
        payload={"dispatch_only": True},
    )
