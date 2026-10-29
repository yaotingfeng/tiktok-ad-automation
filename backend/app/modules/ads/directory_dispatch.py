"""定向回读与批量目录共用领取合同，但不共用批量读取的排队位置。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

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


def targeted_capacity(session, now=None, *, run=None) -> int:
    """生产者与扫描器共用窗口锁；按真实可领取的对方需求保留半窗、其余互借。"""
    from app.modules.ads.directory_services import (
        SERVICE_KEY,
        STATUS_SERVICE,
        directory_service,
    )
    from app.modules.ads.material_directory_children import MATERIAL_CHILD_KEY

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
    own_status = run is not None and directory_service(run) == STATUS_SERVICE
    # 一次活动快照同时计算在途和可借用份额；不能为每个服务再扫积压及历史消息。
    counts = (
        session.connection()
        .execute(
            text("""
        WITH active AS MATERIALIZED (
          SELECT r.id,r.tenant_id,r.claim_generation,r.next_page,r.claimed_until,
            r.next_attempt_at,r.channel,
            (NOT r.query ? :child AND coalesce(r.query->>:service='status',false)) AS status_service
          FROM ad_directory_run r
          WHERE r.workload='live' AND r.status IN ('QUEUED','RUNNING','WAITING_REMOTE')
            AND (coalesce(jsonb_array_length(r.query->'ids'),0)>0
              OR coalesce(jsonb_array_length(r.query->'parent_ids'),0)>0)
        ), delivery AS MATERIALIZED (
          SELECT a.*,p.id IS NOT NULL AS enqueued,p.published_at
          FROM active a LEFT JOIN LATERAL (
            SELECT p.id,p.published_at FROM pending_dispatch p
            WHERE p.tenant_id=a.tenant_id AND p.task_key=(
              'ads.targeted_step:'||a.id::text||':'||a.claim_generation::text||':page:'||a.next_page::text)
            LIMIT 1
          ) p ON true
        ), classified AS (
          SELECT *,coalesce((enqueued AND (published_at IS NULL OR published_at>:cutoff))
            OR claimed_until>:now,false) AS inflight,
            channel='OFFICIAL_API' AND (next_attempt_at IS NULL OR next_attempt_at<=:now)
              AND ((claimed_until IS NULL AND (NOT enqueued OR published_at<:cutoff))
                OR claimed_until<=:now) AS eligible
          FROM delivery
        )
        SELECT count(*) FILTER (WHERE inflight) AS total,
          count(*) FILTER (WHERE inflight AND status_service<>:own_status) AS opposite_inflight,
          count(*) FILTER (WHERE eligible AND status_service<>:own_status) AS opposite_pending
        FROM classified
    """),
            {
                "now": now,
                "cutoff": now - timedelta(minutes=2),
                "child": MATERIAL_CHILD_KEY,
                "service": SERVICE_KEY,
                "own_status": own_status,
            },
        )
        .mappings()
        .one()
    )
    available = max(0, TARGETED_DISPATCH_WINDOW - counts["total"])
    if run is None or TARGETED_DISPATCH_WINDOW < 2:
        return available
    opposite_share = (
        TARGETED_DISPATCH_WINDOW - TARGETED_DISPATCH_WINDOW // 2
        if own_status
        else TARGETED_DISPATCH_WINDOW // 2
    )
    reserved = min(
        counts["opposite_pending"], max(0, opposite_share - counts["opposite_inflight"])
    )
    return max(0, available - reserved)


def is_targeted(run) -> bool:
    return bool(run.query.get("ids") or run.query.get("parent_ids"))


def is_live_directory(run) -> bool:
    return is_targeted(run) and run.workload == "live"


def directory_step_key(run) -> str:
    prefix = TARGETED_TASK_NAME if is_live_directory(run) else TASK_NAME
    return f"{prefix}:{run.id}:{run.claim_generation}:page:{run.next_page}"


def enqueue_directory_step(
    session, run, *, publish_only=False, initial=False, available_at=None
) -> None:
    if initial and (
        not is_live_directory(run) or not targeted_capacity(session, run=run)
    ):
        return
    dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TARGETED_TASK_NAME
        if is_live_directory(run)
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
