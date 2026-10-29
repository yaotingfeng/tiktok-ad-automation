"""刷新候选使用有界 outbox 分片；初次候选沿用 DraftPreparation 的有界调度。"""

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
from app.modules.builds.image_composition_state import ImageCandidateSnapshot
from app.modules.builds.models import BuildDraft

TASK_NAME = "builds.image_candidates"
register_dispatch_task(TASK_NAME, "builds")


def queue_candidate_snapshot(session, snapshot):
    assert snapshot.actor_id is not None and snapshot.preparation_id is None
    snapshot.dispatch_id = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=snapshot.tenant_id, actor_id=snapshot.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"image-candidates:{snapshot.id}:{snapshot.generation}",
        payload={"snapshot_id": str(snapshot.id), "generation": snapshot.generation},
    )
    snapshot.repair_after = datetime.now(UTC) + timedelta(seconds=120)


def process_candidate_snapshot(*, database_engine, tenant_id, actor_id, payload):
    from .image_candidate_service import advance_candidate_snapshot

    if (
        set(payload) != {"snapshot_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 0
    ):
        raise DomainError("dispatch_payload_invalid", "候选构建参数无效")
    try:
        identity = UUID(payload["snapshot_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "候选构建标识无效") from None
    with Session(database_engine) as session, session.begin():
        snapshot = session.get(ImageCandidateSnapshot, identity)
        if (
            snapshot is None
            or snapshot.tenant_id != tenant_id
            or snapshot.actor_id != actor_id
            or snapshot.preparation_id is not None
        ):
            raise DomainError("image_composition_not_found", "候选构建任务不存在")
        advance_candidate_snapshot(session, identity, payload["generation"])


def repair_candidate_snapshots(*, database_engine, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now, count = datetime.now(UTC), 0
    with Session(database_engine) as session, session.begin():
        ids = session.exec(
            select(ImageCandidateSnapshot.id, ImageCandidateSnapshot.draft_id)
            .where(
                ImageCandidateSnapshot.status == "PENDING",
                col(ImageCandidateSnapshot.preparation_id).is_(None),
                col(ImageCandidateSnapshot.repair_after) <= now,
            )
            .order_by(col(ImageCandidateSnapshot.repair_after))
            .limit(limit)
        ).all()
        for identity, draft_id in ids:
            if (
                session.exec(
                    select(BuildDraft)
                    .where(BuildDraft.id == draft_id)
                    .with_for_update(skip_locked=True)
                ).first()
                is None
            ):
                continue
            snapshot = session.exec(
                select(ImageCandidateSnapshot)
                .where(ImageCandidateSnapshot.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if (
                snapshot.status != "PENDING"
                or snapshot.repair_after is None
                or snapshot.repair_after > now
            ):
                continue
            dispatch = (
                session.get(PendingDispatch, snapshot.dispatch_id)
                if snapshot.dispatch_id
                else None
            )
            if dispatch is None:
                queue_candidate_snapshot(session, snapshot)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (
                snapshot.tenant_id,
                snapshot.actor_id,
                TASK_NAME,
                {"snapshot_id": str(snapshot.id), "generation": snapshot.generation},
            ):
                snapshot.status, snapshot.error_code = (
                    "FAILED",
                    "dispatch_payload_invalid",
                )
            elif dispatch.published_at is not None:
                dispatch.published_at, dispatch.available_at = None, now
            snapshot.repair_after = (
                now + timedelta(seconds=120) if snapshot.status == "PENDING" else None
            )
            count += 1
    return count


@celery_app.task(
    name=TASK_NAME,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
    reject_on_worker_lost=True,
)
def image_candidate_task(*, tenant_id: str, actor_id: str, payload: dict):
    process_candidate_snapshot(
        database_engine=engine,
        tenant_id=UUID(tenant_id),
        actor_id=UUID(actor_id),
        payload=payload,
    )


@celery_app.task(name="builds.repair_image_candidates")
def repair_image_candidates_task():
    return repair_candidate_snapshots(database_engine=engine)
