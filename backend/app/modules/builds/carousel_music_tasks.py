"""配乐消息只携带引用；租约过期重放同一代，不清空任何 Redis 队列。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from redis import Redis
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_many_after_commit
from app.jobs.tasks import register_dispatch_task

from .carousel_music_models import MusicSelectionJob

TASK_NAME = "builds.carousel_music"
register_dispatch_task(TASK_NAME, "builds")


def queue_music(session, job, *, delay=0):
    from .carousel_music import _scope

    due = datetime.now(UTC) + timedelta(seconds=delay)
    job.dispatch_id = enqueue_many_after_commit(
        session,
        context=_scope(job).context,
        entries=[
            (
                TASK_NAME,
                f"carousel-music:{job.id}:{job.generation}",
                {"job_id": str(job.id), "generation": job.generation},
                due,
            )
        ],
    )[0]
    job.repair_after = due + timedelta(seconds=120)


def process_music(*, database_engine, redis_client, tenant_id, actor_id, payload):
    from .carousel_music import advance_music

    if (
        set(payload) != {"job_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 1
    ):
        raise DomainError("dispatch_payload_invalid", "配乐任务参数无效")
    try:
        identity = UUID(payload["job_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "配乐任务标识无效") from None
    with Session(database_engine) as session:
        job = session.get(MusicSelectionJob, identity)
        if job is None or (job.tenant_id, job.actor_id) != (tenant_id, actor_id):
            raise DomainError("image_composition_not_found", "配乐任务不存在")
    return advance_music(
        identity,
        payload["generation"],
        database_engine=database_engine,
        redis_client=redis_client,
    )


def repair_music(*, database_engine, limit=100):
    from .carousel_music import _locked

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now, count = datetime.now(UTC), 0
    with Session(database_engine) as session, session.begin():
        ids = session.exec(
            select(MusicSelectionJob.id)
            .where(
                col(MusicSelectionJob.status).in_(["PENDING", "RUNNING"]),
                col(MusicSelectionJob.repair_after) <= now,
            )
            .order_by(col(MusicSelectionJob.repair_after))
            .limit(limit)
        ).all()
        for identity in ids:
            from .models import BuildDraft

            current = session.get(MusicSelectionJob, identity)
            assert current is not None
            if (
                session.exec(
                    select(BuildDraft)
                    .where(BuildDraft.id == current.draft_id)
                    .with_for_update(skip_locked=True)
                ).first()
                is None
            ):
                continue
            job = _locked(session, identity)
            if (
                job.status not in {"PENDING", "RUNNING"}
                or job.repair_after is None
                or job.repair_after > now
            ):
                continue
            dispatch = (
                session.get(PendingDispatch, job.dispatch_id)
                if job.dispatch_id
                else None
            )
            expected = {"job_id": str(job.id), "generation": job.generation}
            if dispatch is None:
                queue_music(session, job)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (job.tenant_id, job.actor_id, TASK_NAME, expected):
                job.status, job.error_code = "FAILED", "dispatch_payload_invalid"
            else:
                dispatch.published_at, dispatch.available_at = None, now
            job.status = "PENDING" if job.status == "RUNNING" else job.status
            job.claim = None
            job.repair_after = now + timedelta(seconds=120)
            count += 1
    return count


@celery_app.task(
    name=TASK_NAME,
    time_limit=30,
    soft_time_limit=25,
    acks_late=True,
    reject_on_worker_lost=True,
)
def carousel_music_task(*, tenant_id: str, actor_id: str, payload: dict):
    with Redis.from_url(settings.REDIS_URL) as redis:
        return process_music(
            database_engine=engine,
            redis_client=redis,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )


@celery_app.task(name="builds.repair_carousel_music")
def repair_carousel_music_task():
    return {
        "jobs": repair_music(database_engine=engine),
        "preparations": repair_music_preparation(database_engine=engine),
    }


PREPARE_TASK = "builds.prepare_carousel_music"
register_dispatch_task(PREPARE_TASK, "builds")


def _queue_preparation(session, run):
    from .carousel_music import _scope

    run.dispatch_id = enqueue_many_after_commit(
        session,
        context=_scope(run).context,
        entries=[
            (
                PREPARE_TASK,
                f"carousel-music-prep:{run.id}:{run.generation}",
                {"run_id": str(run.id), "generation": run.generation},
                None,
            )
        ],
    )[0]
    run.repair_after = datetime.now(UTC) + timedelta(seconds=120)


def queue_music_preparation(session, scope, draft):
    from .carousel_music_models import MusicPreparationRun

    run = MusicPreparationRun(
        tenant_id=scope.tenant_id,
        draft_id=draft.id,
        bc_id=scope.bc_id,
        actor_id=scope.actor_id,
        active_generation=draft.active_image_generation,
        route=scope.route.model_dump(mode="json"),
    )
    session.add(run)
    session.flush()
    _queue_preparation(session, run)
    return run.id


def advance_music_preparation(session, run_id, generation):
    from time import monotonic

    from .carousel_music import (
        _authority,
        _scope,
        enqueue_music,
        request_unit_recommendation,
    )
    from .carousel_music_models import MusicPreparationRun
    from .image_composition_models import DraftCreativeUnit
    from .models import BuildDraft

    run = session.get(MusicPreparationRun, run_id)
    if run is None:
        raise DomainError("image_composition_not_found", "配乐准备不存在")
    session.exec(
        select(BuildDraft).where(BuildDraft.id == run.draft_id).with_for_update()
    ).one()
    run = session.exec(
        select(MusicPreparationRun)
        .where(MusicPreparationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if run.status != "PENDING" or run.generation != generation:
        return {"status": run.status, "processed": run.processed}
    started = monotonic()
    try:
        scope = _scope(run)
        draft = _authority(session, scope, lock=True)
        if draft.active_image_generation != run.active_generation or (
            run.action == "RETRY_FAILED" and draft.revision != run.base_revision
        ):
            run.status, run.error_code, run.repair_after = (
                "CANCELLED",
                "draft_revision_conflict",
                None,
            )
        else:
            query = select(DraftCreativeUnit.id).where(
                DraftCreativeUnit.tenant_id == run.tenant_id,
                DraftCreativeUnit.draft_id == run.draft_id,
                DraftCreativeUnit.generation == run.active_generation,
            )
            if run.action == "RETRY_FAILED":
                from .models import DraftInput

                query = query.where(
                    col(DraftCreativeUnit.drama_id).in_(
                        [UUID(i) for i in run.drama_ids]
                    ),
                    select(DraftInput.id)
                    .where(
                        DraftInput.tenant_id == run.tenant_id,
                        DraftInput.draft_id == run.draft_id,
                        DraftInput.kind == "drama",
                        DraftInput.drama_id == DraftCreativeUnit.drama_id,
                    )
                    .exists(),
                )
            else:
                query = query.where(
                    col(DraftCreativeUnit.music_status).in_(["PENDING", "RUNNING"])
                )
            if run.after_id:
                query = query.where(DraftCreativeUnit.id > run.after_id)
            ids = session.exec(
                query.order_by(col(DraftCreativeUnit.id)).limit(20)
            ).all()
            for identity in ids:
                if run.action == "RETRY_FAILED":
                    unit = session.get(
                        DraftCreativeUnit,
                        (run.tenant_id, run.draft_id, run.active_generation, identity),
                    )
                    if unit.music_protected:
                        run.skipped_protected += 1
                    elif unit.music_status != "FAILED":
                        run.skipped_recovered += 1
                    else:
                        request_unit_recommendation(
                            session,
                            scope,
                            draft,
                            unit,
                            uuid5(run.id, str(identity)),
                            "RETRY",
                        )
                        run.retried += 1
                else:
                    enqueue_music(session, scope, draft, unit_ids=[identity])
                run.after_id, run.processed = identity, run.processed + 1
                if monotonic() - started >= 10:
                    break
            if len(ids) < 20 and (not ids or run.after_id == ids[-1]):
                run.status, run.repair_after = "READY", None
            else:
                run.generation += 1
                _queue_preparation(session, run)
    except DomainError as error:
        run.status, run.error_code, run.repair_after = "FAILED", error.code, None
    return {"status": run.status, "processed": run.processed}


@celery_app.task(
    name=PREPARE_TASK,
    time_limit=30,
    soft_time_limit=25,
    acks_late=True,
    reject_on_worker_lost=True,
)
def prepare_carousel_music_task(*, tenant_id: str, actor_id: str, payload: dict):
    from .carousel_music_models import MusicPreparationRun

    if (
        set(payload) != {"run_id", "generation"}
        or type(payload["generation"]) is not int
        or payload["generation"] < 1
    ):
        raise DomainError("dispatch_payload_invalid", "配乐准备参数无效")
    try:
        identity = UUID(payload["run_id"])
    except ValueError, TypeError, AttributeError:
        raise DomainError("dispatch_payload_invalid", "配乐准备标识无效") from None
    with Session(engine) as session, session.begin():
        run = session.get(MusicPreparationRun, identity)
        if run is None or (str(run.tenant_id), str(run.actor_id)) != (
            tenant_id,
            actor_id,
        ):
            raise DomainError("image_composition_not_found", "配乐准备不存在")
        return advance_music_preparation(session, identity, payload["generation"])


def repair_music_preparation(*, database_engine, limit=100):
    from .carousel_music_models import MusicPreparationRun
    from .models import BuildDraft

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("repair limit must be between 1 and 100")
    now, count = datetime.now(UTC), 0
    with Session(database_engine) as session, session.begin():
        ids = session.exec(
            select(MusicPreparationRun.id)
            .where(
                MusicPreparationRun.status == "PENDING",
                col(MusicPreparationRun.repair_after) <= now,
            )
            .order_by(col(MusicPreparationRun.repair_after))
            .limit(limit)
        ).all()
        for identity in ids:
            run = session.get(MusicPreparationRun, identity)
            assert run is not None
            if (
                session.exec(
                    select(BuildDraft)
                    .where(BuildDraft.id == run.draft_id)
                    .with_for_update(skip_locked=True)
                ).first()
                is None
            ):
                continue
            run = session.exec(
                select(MusicPreparationRun)
                .where(MusicPreparationRun.id == identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if (
                run.status != "PENDING"
                or run.repair_after is None
                or run.repair_after > now
            ):
                continue
            dispatch = (
                session.get(PendingDispatch, run.dispatch_id)
                if run.dispatch_id
                else None
            )
            if dispatch is None:
                _queue_preparation(session, run)
            elif (
                dispatch.tenant_id,
                dispatch.actor_id,
                dispatch.task_name,
                dispatch.payload,
            ) != (
                run.tenant_id,
                run.actor_id,
                PREPARE_TASK,
                {"run_id": str(run.id), "generation": run.generation},
            ):
                run.status, run.error_code = "FAILED", "dispatch_payload_invalid"
            else:
                dispatch.published_at, dispatch.available_at = None, now
            run.repair_after = now + timedelta(seconds=120)
            count += 1
    return count
