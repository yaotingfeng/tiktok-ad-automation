"""搭建页面的小程序目录与选择；GET 只读缓存，POST 仅修改本地草稿。"""

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.routing import freeze_route
from app.modules.providers.models import PromotionLink
from app.modules.tenants.permissions import require_tenant

from .mini_targets import (
    MiniTarget,
    explicit_mini_id,
    link_url,
    matching_link_id,
    remember_target,
    url_key,
)
from .models import BuildDraft, DraftAccount, DraftDrama
from .scene_job_models import SceneJob, SceneJobPage


class MiniOption(BaseModel):
    minis_id: str
    name: str


class DraftMinis(BaseModel):
    state: Literal["pending", "choose", "selected", "unavailable", "conflict"]
    catalog_job_id: UUID | None = None
    advertiser_id: str | None = None
    selected: MiniOption | None = None
    items: list[MiniOption] = Field(default_factory=list)
    next_page: int | None = None


class ChooseMiniRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(gt=0, strict=True)
    catalog_job_id: UUID
    minis_id: str = Field(min_length=1, max_length=128)


def catalog_options(
    session: Session,
    job: SceneJob,
    *,
    page: int | None = None,
    minis_id: str | None = None,
) -> list[dict]:
    query = select(SceneJobPage).where(
        SceneJobPage.tenant_id == job.tenant_id,
        SceneJobPage.job_id == job.id,
        SceneJobPage.resource == "minis",
    )
    if page is not None:
        query = query.where(SceneJobPage.page == page)
    if minis_id is not None:
        query = query.where(
            col(SceneJobPage.facts).contains({"options": [{"minis_id": minis_id}]})
        )
    rows = session.exec(query.order_by(col(SceneJobPage.page)).limit(1)).all()
    return [
        item
        for row in rows
        for item in row.facts.get("options", [])
        if item["status"] == "ACTIVE"
        and item["type"] == "MINI_SERIES"
        and item.get("regions")
        and (minis_id is None or item["minis_id"] == minis_id)
    ]


def option(item: dict) -> MiniOption:
    # 名称缺失仍展示真实标识，不能杜撰一个业务名称。
    return MiniOption(
        minis_id=item["minis_id"], name=item.get("name") or "未命名小程序"
    )


def match_catalog_link(
    session: Session, *, context: TenantContext, link_id: UUID, job: SceneJob
) -> bool:
    link = session.get(PromotionLink, link_id)
    if link is None or link.tenant_id != context.tenant_id:
        raise DomainError("resource_not_found", "推广链接不存在")
    candidate = (
        explicit_mini_id(link_url(link))
        or urlsplit(link_url(link)).path.rstrip("/").split("/")[-1]
    )
    values = catalog_options(session, job, minis_id=candidate)
    identity = matching_link_id(link_url(link), {item["minis_id"] for item in values})
    if identity is None:
        return False
    remember_target(
        session, context=context, url=link_url(link), minis_id=identity, source="LINK"
    )
    return True


def _links(
    session: Session, context: TenantContext, draft: BuildDraft
) -> list[PromotionLink]:
    return list(
        session.exec(
            select(PromotionLink)
            .join(
                DraftDrama,
                (col(DraftDrama.tenant_id) == PromotionLink.tenant_id)
                & (col(DraftDrama.link_id) == PromotionLink.id),
            )
            .where(
                DraftDrama.tenant_id == context.tenant_id,
                DraftDrama.draft_id == draft.id,
            )
            .order_by(col(DraftDrama.first_line))
            .limit(1001)
        ).all()
    )


def _catalog(
    session: Session, context: TenantContext, draft: BuildDraft
) -> tuple[str | None, SceneJob | None]:
    account = session.exec(
        select(DraftAccount.advertiser_id)
        .where(
            DraftAccount.tenant_id == context.tenant_id,
            DraftAccount.draft_id == draft.id,
        )
        .order_by(col(DraftAccount.advertiser_id))
        .limit(1)
    ).first()
    if account is None:
        return None, None
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    return account, account_catalog(
        session, context=context, account=account, route=route
    )


def account_catalog(
    session: Session, *, context: TenantContext, account: str, route: FrozenTikTokRoute
) -> SceneJob | None:
    from .scene import _account_scope

    job = session.exec(
        select(SceneJob)
        .where(
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.bc_id == route.bc_id,
            SceneJob.advertiser_id == account,
            SceneJob.connection_id == route.connection_id,
            SceneJob.status == "COMPLETE",
            select(SceneJobPage.id)
            .where(SceneJobPage.job_id == SceneJob.id, SceneJobPage.resource == "minis")
            .exists(),
            col(SceneJob.expires_at) > datetime.now(UTC),
            SceneJob.frozen_route == route.model_dump(mode="json"),
        )
        .order_by(col(SceneJob.created_at).desc())
        .limit(1)
    ).first()
    if job is None:
        return None
    scope = _account_scope(
        session,
        context=context,
        bc_id=route.bc_id,
        advertiser_id=account,
        minis_id=job.minis_id,
        route=route,
    )
    return job if scope["basis"] == job.scope_basis else None


def draft_minis(
    session: Session, *, context: TenantContext, draft_id: UUID, page: int = 1
) -> DraftMinis:
    from .drafts import get_draft

    if type(page) is not int or not 1 <= page <= 1000:
        raise DomainError("invalid_pagination", "小程序页码无效")
    draft = get_draft(session, context=context, draft_id=draft_id)
    account, job = _catalog(session, context, draft)
    if job is None:
        return DraftMinis(state="pending", advertiser_id=account)
    links = _links(session, context, draft)
    keys = [url_key(link_url(link)) for link in links]
    saved = {
        row.url_digest: row.minis_id
        for row in session.exec(
            select(MiniTarget).where(
                MiniTarget.tenant_id == context.tenant_id,
                col(MiniTarget.url_digest).in_(keys),
            )
        ).all()
    }
    values = {
        explicit_mini_id(link_url(link)) or saved.get(url_key(link_url(link)))
        for link in links
    }
    selected_id = (
        next(iter(values)) if len(values) == 1 and None not in values else None
    )
    selected = (
        catalog_options(session, job, minis_id=selected_id) if selected_id else []
    )
    items = [option(item) for item in catalog_options(session, job, page=page)]
    pages = job.facts.get("minis", {}).get("total_page", 0)
    return DraftMinis(
        state="selected"
        if selected
        else "conflict"
        if len(values - {None}) > 1
        else "choose"
        if pages
        else "unavailable",
        catalog_job_id=job.id,
        advertiser_id=account,
        selected=option(selected[0]) if selected else None,
        items=items,
        next_page=page + 1 if page < pages else None,
    )


def choose_mini(
    session: Session, *, context: TenantContext, draft_id: UUID, body: ChooseMiniRequest
) -> int:
    from .drafts import _bump_revision, get_draft
    from .mutations import _apply

    def apply() -> int:
        draft = get_draft(session, context=context, draft_id=draft_id, lock=True)
        if draft.revision != body.expected_revision:
            raise DomainError("draft_revision_conflict", "草稿已更新，请刷新后重新选择")
        if draft.status != "READY":
            raise DomainError("draft_not_ready", "请等待当前账户和剧目准备完成")
        _, job = _catalog(session, context, draft)
        if job is None or job.id != body.catalog_job_id:
            raise DomainError(
                "minis_catalog_stale", "小程序目录已更新或过期，请重新准备"
            )
        if not catalog_options(session, job, minis_id=body.minis_id):
            raise DomainError("minis_unavailable", "所选小程序不在当前账户的可用目录")
        links = _links(session, context, draft)
        if not links or len(links) > 1000:
            raise DomainError("minis_links_unavailable", "请先完成剧目推广链接准备")
        # 一次查出链接路径中与真实目录完全一致的 Mini，避免逐链接查询或猜短码。
        candidates = list(
            {urlsplit(link_url(link)).path.rstrip("/").split("/")[-1] for link in links}
        )
        available = set(
            session.execute(
                text("""
            SELECT DISTINCT item->>'minis_id' FROM build_scene_job_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.facts->'options') item
            WHERE p.tenant_id=:tenant_id AND p.job_id=:job_id AND p.resource='minis'
              AND item->>'minis_id'=ANY(:candidates)
        """),
                {
                    "tenant_id": context.tenant_id,
                    "job_id": job.id,
                    "candidates": candidates,
                },
            ).scalars()
        )
        for link in links:
            direct = explicit_mini_id(link_url(link)) or matching_link_id(
                link_url(link), available
            )
            if direct and direct != body.minis_id:
                raise DomainError(
                    "minis_link_conflict",
                    "所选小程序与推广链接明确指向不一致，请分开搭建",
                )
        # 一次选择确认本批次全部链接的目标。按链接去重后批量写入，不绑定版权方应用。
        rows = {
            url_key(link_url(link)): {
                "tenant_id": context.tenant_id,
                "url_digest": url_key(link_url(link)),
                "minis_id": body.minis_id,
                "source": "USER",
                "actor_id": context.actor_id,
                "confirmed_at": datetime.now(UTC),
            }
            for link in links
        }
        statement = insert(MiniTarget).values(list(rows.values()))
        session.exec(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "url_digest"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in ("minis_id", "source", "actor_id", "confirmed_at")
                },
            )
        )
        draft.status = "DRAFT"
        return _bump_revision(session, draft, body.expected_revision)

    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    return _apply(
        session,
        context=context,
        draft_id=draft_id,
        request_id=body.request_id,
        kind="minis",
        intent=body.model_dump(exclude={"request_id"}),
        operation=apply,
    )


def reuse_minis_catalog(
    session: Session, *, context: TenantContext, job: SceneJob, route: FrozenTikTokRoute
) -> None:
    if job.minis_id is None:
        return
    source = account_catalog(
        session, context=context, account=job.advertiser_id, route=route
    )
    if source is None:
        return
    selected = catalog_options(session, source, minis_id=job.minis_id)
    if len(selected) != 1:
        return
    # 原请求是账户全量目录，指定 Mini 的筛选只发生在本地，复用原观察期限和来源任务。
    job.facts = {
        "minis": {**source.facts["minis"], "matches": selected},
        "minis_catalog_job_id": str(source.id),
    }
    job.first_observed_at = source.first_observed_at
    job.expires_at = source.expires_at
