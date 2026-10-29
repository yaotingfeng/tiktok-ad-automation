"""浏览器只提供草稿与音乐意图；所有平台账户及连接由服务端解析。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field
from redis import Redis

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant

from . import carousel_music as service
from .carousel_music_models import MusicSelectionJob
from .image_composition_api import _scope
from .image_composition_schemas import ImmutableModel

router = APIRouter(prefix="/build-drafts/{draft_id}/music")


class MusicSearch(ImmutableModel):
    keyword: str = Field(min_length=1, max_length=1000)
    cursor: str | None = Field(default=None, max_length=4096)


class MusicRead(ImmutableModel):
    music_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


class MusicRequest(ImmutableModel):
    request_id: UUID
    expected_content_revision: int = Field(ge=1, strict=True)
    expected_music_revision: int = Field(ge=0, strict=True)
    action: Literal["RECOMMEND", "RETRY", "SET_MANUAL"]
    music_id: str | None = Field(default=None, min_length=1, max_length=128)


def _browse(session, user, tenant_id, draft_id, **kwargs):
    context = require_tenant(
        session, actor_id=user.id, tenant_id=tenant_id, action="read"
    )
    engine = session.get_bind()
    # 请求依赖会话不跨网络占连接/事务；工作会话在服务内有独立返回期核验。
    session.rollback()
    with Redis.from_url(settings.REDIS_URL) as redis:
        return service.browse_music(
            database_engine=engine,
            redis_client=redis,
            context=context,
            draft_id=draft_id,
            **kwargs,
        )


@router.get("/search")
def search_music(
    tenant_id: UUID,
    draft_id: UUID,
    query: Annotated[MusicSearch, Query()],
    session: SessionDep,
    user: CurrentUser,
):
    return _browse(session, user, tenant_id, draft_id, **query.model_dump())


@router.post("/read")
def read_music(
    tenant_id: UUID,
    draft_id: UUID,
    body: MusicRead,
    session: SessionDep,
    user: CurrentUser,
):
    return _browse(session, user, tenant_id, draft_id, music_ids=body.music_ids)


@router.post("/units/{unit_id}")
def request_music(
    tenant_id: UUID,
    draft_id: UUID,
    unit_id: UUID,
    body: MusicRequest,
    session: SessionDep,
    user: CurrentUser,
):
    result = service.request_music(
        session,
        _scope(session, user, tenant_id, draft_id, write=True),
        unit_id,
        **body.model_dump(),
    )
    session.commit()
    return result


@router.get("/jobs/{job_id}")
def music_progress(
    tenant_id: UUID,
    draft_id: UUID,
    job_id: UUID,
    session: SessionDep,
    user: CurrentUser,
):
    _scope(session, user, tenant_id, draft_id)
    job = session.get(MusicSelectionJob, job_id)
    if job is None or (job.tenant_id, job.draft_id) != (tenant_id, draft_id):
        raise DomainError("image_composition_not_found", "配乐任务不存在")
    return service._public(job)


@router.get("")
def music_summary(
    tenant_id: UUID, draft_id: UUID, session: SessionDep, user: CurrentUser
):
    from sqlalchemy import func
    from sqlmodel import col, select

    from .carousel_music_models import MusicPreparationRun
    from .models import BuildDraft

    _scope(session, user, tenant_id, draft_id)
    draft = session.get(BuildDraft, draft_id)
    assert draft is not None
    groups = {}
    for model, key in (
        (MusicSelectionJob, "jobs"),
        (MusicPreparationRun, "preparations"),
    ):
        rows = session.exec(
            select(model.status, func.count())
            .where(
                model.tenant_id == tenant_id,
                model.draft_id == draft_id,
                model.active_generation == draft.active_image_generation,
            )
            .group_by(model.status)
        ).all()
        groups[key] = dict(rows)
    failed = session.exec(
        select(MusicSelectionJob.id, MusicSelectionJob.error_code)
        .where(
            MusicSelectionJob.tenant_id == tenant_id,
            MusicSelectionJob.draft_id == draft_id,
            MusicSelectionJob.active_generation == draft.active_image_generation,
            MusicSelectionJob.status == "FAILED",
        )
        .order_by(col(MusicSelectionJob.id))
        .limit(10)
    ).all()
    preparation_errors = session.exec(
        select(MusicPreparationRun.id, MusicPreparationRun.error_code)
        .where(
            MusicPreparationRun.tenant_id == tenant_id,
            MusicPreparationRun.draft_id == draft_id,
            MusicPreparationRun.active_generation == draft.active_image_generation,
            MusicPreparationRun.status == "FAILED",
        )
        .order_by(col(MusicPreparationRun.id))
        .limit(10)
    ).all()
    return {
        "active_generation": draft.active_image_generation,
        **groups,
        "preparation_errors": [
            {"run_id": str(identity), "error_code": code}
            for identity, code in preparation_errors
        ],
        "recent_errors": [
            {"job_id": str(identity), "error_code": code} for identity, code in failed
        ],
    }
