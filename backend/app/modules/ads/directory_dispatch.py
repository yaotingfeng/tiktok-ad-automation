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
TARGETED_DISPATCH_WINDOW = 8


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


def enqueue_directory_step(session, run, *, publish_only=False, initial=False) -> None:
    if initial and is_targeted(run) and not targeted_capacity(session):
        return
    enqueue_after_commit(
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
