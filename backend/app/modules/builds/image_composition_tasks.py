"""仅本地结构生成的 outbox 消费和恢复；不调用配乐/广告 provider。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.builds.image_composition_state import ImageCompositionRun
from app.modules.builds.models import BuildDraft

TASK_NAME = "builds.image_composition"
register_dispatch_task(TASK_NAME, "builds")


def queue_run(session, run):
    run.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"image-composition:{run.id}:{run.generation}:{run.cursor}",
        payload={
            "run_id": str(run.id),
            "generation": run.generation,
            "cursor": run.cursor,
        },
    )
    run.repair_after = datetime.now(UTC) + timedelta(seconds=120)


def process_composition(*, database_engine, tenant_id, actor_id, payload):
    from app.modules.builds.image_composition_service import advance_composition

    if set(payload) != {"run_id", "generation", "cursor"} or any(
        type(payload[k]) is not int or payload[k] < (1 if k == "generation" else 0)
        for k in ("generation", "cursor")
    ):
        raise DomainError("dispatch_payload_invalid", "编排任务参数无效")
    try:
        identity = UUID(payload["run_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "编排任务标识无效") from None
    with Session(database_engine) as session, session.begin():
        run = session.get(ImageCompositionRun, identity)
        if run is None or run.tenant_id != tenant_id or run.actor_id != actor_id:
            raise DomainError("image_composition_not_found", "编排任务不存在")
        session.exec(
            select(BuildDraft)
            .where(BuildDraft.tenant_id == tenant_id, BuildDraft.id == run.draft_id)
            .with_for_update()
        ).one()
        session.refresh(run)
        # 旧消息不能替代当前分页片；当前消息可随 worker 中止后原样重放。
        if run.cursor != payload["cursor"]:
            return
        advance_composition(session, identity, payload["generation"])


def repair_compositions(*, database_engine, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now, count = datetime.now(UTC), 0
    with Session(database_engine) as session, session.begin():
        ids = session.exec(
            select(ImageCompositionRun.id, ImageCompositionRun.draft_id)
            .where(
                ImageCompositionRun.status == "PENDING",
                ImageCompositionRun.repair_after <= now,
            )
            .order_by(col(ImageCompositionRun.repair_after))
            .limit(limit)
        ).all()
        for identity, draft_id in ids:
            # 所有路径固定 parent→run→outbox 锁顺序，避免恢复与取消相互死锁。
            if (
                session.exec(
                    select(BuildDraft)
                    .where(BuildDraft.id == draft_id)
                    .with_for_update(skip_locked=True)
                ).first()
                is None
            ):
                continue
            run = session.exec(
                select(ImageCompositionRun)
                .where(ImageCompositionRun.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if run.status != "PENDING" or run.repair_after > now:
                continue
            dispatch = (
                session.get(PendingDispatch, run.dispatch_id)
                if run.dispatch_id
                else None
            )
            if dispatch is None:
                queue_run(session, run)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (
                run.tenant_id,
                run.actor_id,
                TASK_NAME,
                {
                    "run_id": str(run.id),
                    "generation": run.generation,
                    "cursor": run.cursor,
                },
            ):
                run.status, run.error_code = "FAILED", "dispatch_payload_invalid"
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
            run.repair_after = now + timedelta(seconds=120)
            count += 1
    return count


@celery_app.task(
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def image_composition_task(*, tenant_id: str, actor_id: str, payload: dict):
    process_composition(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name="builds.repair_image_compositions")
def repair_image_compositions_task():
    return repair_compositions(database_engine=engine)
