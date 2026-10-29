"""所选剧失败重试：开始时记录用户意图，已有准备任务按有界片推进。"""

from uuid import UUID

from sqlmodel import col, select

from app.core.errors import DomainError

from .carousel_music_models import MusicPreparationRun
from .carousel_music_tasks import _queue_preparation
from .draft_catalog import current_image_drama_clause
from .image_composition_service import _draft, _editable, _receipt, _request
from .models import DraftDrama


def public_batch(run):
    return {
        "run_id": str(run.id),
        "revision": run.base_revision,
        "status": run.status,
        "drama_ids": run.drama_ids,
        "processed": run.processed,
        "retried": run.retried,
        "skipped_protected": run.skipped_protected,
        "skipped_recovered": run.skipped_recovered,
        "error_code": run.error_code,
    }


def start_music_retry(session, scope, request_id, expected_revision, drama_ids):
    ids = sorted({str(UUID(str(identity))) for identity in drama_ids})
    draft, fingerprint, replay = _request(
        session,
        scope,
        request_id,
        "music-batch-retry",
        expected_revision,
        {"drama_ids": ids},
    )
    if replay is not None:
        return replay
    _editable(session, draft)
    if not 1 <= len(ids) <= 100:
        raise DomainError("music_request_invalid", "每次请选择1至100部剧")
    current = session.exec(
        select(DraftDrama.drama_id).where(
            DraftDrama.tenant_id == scope.tenant_id,
            DraftDrama.draft_id == scope.draft_id,
            col(DraftDrama.drama_id).in_([UUID(i) for i in ids]),
            current_image_drama_clause(),
        )
    ).all()
    if set(map(str, current)) != set(ids) or not draft.active_image_generation:
        raise DomainError("music_request_invalid", "所选剧目不属于当前图片草稿")
    from .drafts import _bump_revision

    was_applied = draft.applied_image_rule_revision == draft.revision
    _bump_revision(session, draft, draft.revision)
    if was_applied:
        draft.applied_image_rule_revision = draft.revision
    run = MusicPreparationRun(
        tenant_id=scope.tenant_id,
        draft_id=draft.id,
        bc_id=scope.bc_id,
        actor_id=scope.actor_id,
        active_generation=draft.active_image_generation,
        route=scope.route.model_dump(mode="json"),
        action="RETRY_FAILED",
        drama_ids=ids,
        base_revision=draft.revision,
    )
    session.add(run)
    session.flush()
    _queue_preparation(session, run)
    return _receipt(session, scope, request_id, fingerprint, public_batch(run))


def batch_progress(session, scope, run_id):
    _draft(session, scope, verify=False)
    run = session.get(MusicPreparationRun, UUID(str(run_id)))
    if run is None or (run.tenant_id, run.draft_id, run.action) != (
        scope.tenant_id,
        scope.draft_id,
        "RETRY_FAILED",
    ):
        raise DomainError("image_composition_not_found", "批量重试不存在")
    return public_batch(run)


def recent_batches(session, scope):
    _draft(session, scope, verify=False)
    return [
        public_batch(run)
        for run in session.exec(
            select(MusicPreparationRun)
            .where(
                MusicPreparationRun.tenant_id == scope.tenant_id,
                MusicPreparationRun.draft_id == scope.draft_id,
                MusicPreparationRun.action == "RETRY_FAILED",
            )
            .order_by(
                col(MusicPreparationRun.base_revision).desc(),
                col(MusicPreparationRun.id),
            )
            .limit(20)
        ).all()
    ]
