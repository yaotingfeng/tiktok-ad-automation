"""按当前路由读取本地场景目录；集合聚合不触发远端查询，也不丢弃未知账户。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import usable_grants
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.tenants.permissions import require_tenant

from .models import DraftAccount
from .scene_job_models import SceneJob
from .targeting_schemas import TargetingAccount, TargetingDirectory


def _eligible(
    context: TenantContext,
    route: FrozenTikTokRoute,
    draft_id: UUID | None,
    minis_id: str | None,
):
    now = datetime.now(UTC)
    grants = usable_grants(
        tenant_id=context.tenant_id, bc_id=route.bc_id, action="read"
    ).subquery()
    authorization = (
        select(ConnectionAuthorization.id)
        .where(
            ConnectionAuthorization.tenant_id == context.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
            col(ConnectionAuthorization.verified_at) <= now,
            ConnectionAuthorization.source != "",
            ConnectionAuthorization.source != "UNKNOWN",
            col(ConnectionAuthorization.permission_summary)["read_authorized"]
            .as_boolean()
            .is_(True),
        )
        .exists()
    )
    query = (
        select(SceneJob)
        .join(
            grants,
            and_(
                col(SceneJob.advertiser_id) == grants.c.advertiser_id,
                col(SceneJob.connection_id) == grants.c.connection_id,
            ),
        )
        .where(
            grants.c.checked_at <= now,
            authorization,
            SceneJob.tenant_id == context.tenant_id,
            SceneJob.bc_id == route.bc_id,
            col(SceneJob.connection_id) == route.connection_id,
        )
    )
    if draft_id is not None:
        query = query.join(
            DraftAccount,
            and_(
                col(DraftAccount.tenant_id) == SceneJob.tenant_id,
                col(DraftAccount.draft_id) == draft_id,
                col(DraftAccount.advertiser_id) == SceneJob.advertiser_id,
                col(DraftAccount.connection_id) == SceneJob.connection_id,
            ),
        )
    if minis_id is not None:
        query = query.where(SceneJob.minis_id == minis_id)
    latest = (
        query.distinct(col(SceneJob.advertiser_id), col(SceneJob.minis_id))
        .order_by(
            col(SceneJob.advertiser_id),
            col(SceneJob.minis_id),
            col(SceneJob.created_at).desc(),
            col(SceneJob.id).desc(),
        )
        .cte("latest_targeting_scene")
    )
    # 先选最新任务再筛有效性；最新任务失败/重检中不能借上一次完成结果冒充当前。
    expected = route.model_dump(mode="json", exclude={"binding_revision"})
    minis = (
        func.jsonb_array_elements(latest.c.facts["minis"]["matches"])
        .table_valued("value")
        .alias("target_mini")
    )
    mini = minis.c.value.cast(JSONB)
    valid = (
        select(latest.c.advertiser_id, latest.c.facts, mini.label("mini"))
        .select_from(latest)
        .join(minis, true())
        .where(
            latest.c.status == "COMPLETE",
            latest.c.expires_at > now,
            latest.c.frozen_route.contains(expected),
            func.coalesce(latest.c.frozen_route["binding_revision"].astext, "0")
            == str(route.binding_revision),
            func.jsonb_array_length(latest.c.facts["minis"]["matches"]) == 1,
            mini["minis_id"].astext == latest.c.minis_id,
            mini["status"].astext == "ACTIVE",
            mini["type"].astext == "MINI_SERIES",
        )
        .cte("verified_targeting_scenes")
    )
    locations = (
        func.jsonb_array_elements(valid.c.facts["regions"]["locations"])
        .table_valued("value")
        .alias("target_region")
    )
    location = locations.c.value.cast(JSONB)
    eligible = (
        select(
            valid.c.advertiser_id, location["region_code"].astext.label("region_code")
        )
        .select_from(valid)
        .join(locations, true())
        .where(
            valid.c.mini["regions"].op("?")(location["region_code"].astext),
            location["location_id"].astext != "",
        )
        .distinct()
        .cte("eligible_targeting_regions")
    )
    # 已核实但交集为空是终态不可用，不能混同仍未核实的账户。
    return valid, eligible


def region_directory(
    session: Session,
    *,
    context: TenantContext,
    route: FrozenTikTokRoute,
    draft_id: UUID | None = None,
    minis_id: str | None = None,
) -> TargetingDirectory:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    valid, eligible = _eligible(context, route, draft_id, minis_id)
    verified = session.exec(
        select(func.count(func.distinct(valid.c.advertiser_id)))
    ).one()
    total = (
        session.exec(
            select(func.count())
            .select_from(DraftAccount)
            .where(
                col(DraftAccount.tenant_id) == context.tenant_id,
                col(DraftAccount.draft_id) == draft_id,
                DraftAccount.bc_id == route.bc_id,
            )
        ).one()
        if draft_id
        else verified
    )
    query = (
        select(eligible.c.region_code)
        .group_by(eligible.c.region_code)
        .order_by(eligible.c.region_code)
    )
    if draft_id:
        query = query.having(
            func.count(func.distinct(eligible.c.advertiser_id)) == total
        )
    countries = list(session.exec(query).all()) if total and total == verified else []
    return TargetingDirectory(
        region_codes=countries,
        account_count=total,
        verified_account_count=verified,
        state="READY"
        if countries
        else "PENDING"
        if verified < total
        else "UNAVAILABLE",
    )


def draft_directory(
    session: Session, *, context: TenantContext, draft_id: UUID
) -> TargetingDirectory:
    from .drafts import get_draft
    from .mini_selection import _links, _target_ids
    from .targeting_service import effective_targeting

    draft = get_draft(session, context=context, draft_id=draft_id)
    targets = _target_ids(session, context, _links(session, context, draft))
    if len(targets) != 1 or None in targets or draft.status != "READY":
        return TargetingDirectory(revision=draft.revision)
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    result = region_directory(
        session,
        context=context,
        route=route,
        draft_id=draft.id,
        minis_id=next(iter(targets)),
    )
    result.revision = draft.revision
    result.unavailable_region_codes = sorted(
        set(effective_targeting(session, context, draft).region_codes)
        - set(result.region_codes)
    )
    return result


def account_regions(
    session: Session,
    *,
    context: TenantContext,
    draft_id: UUID,
    after: str = "",
    limit: int = 50,
) -> Page[TargetingAccount]:
    from .drafts import get_draft
    from .mini_selection import _links, _target_ids

    draft = get_draft(session, context=context, draft_id=draft_id)
    targets = _target_ids(session, context, _links(session, context, draft))
    if len(targets) != 1 or None in targets:
        raise DomainError("minis_selection_required", "请先选择本批小程序")
    route = freeze_route(
        session,
        context=context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    _, eligible = _eligible(context, route, draft.id, next(iter(targets)))
    query = select(DraftAccount.advertiser_id).where(
        col(DraftAccount.tenant_id) == context.tenant_id,
        col(DraftAccount.draft_id) == draft.id,
    )
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    ids = session.exec(
        query.where(DraftAccount.advertiser_id > after)
        .order_by(col(DraftAccount.advertiser_id))
        .limit(limit + 1)
    ).all()
    rows = session.exec(
        select(eligible.c.advertiser_id, eligible.c.region_code)
        .where(eligible.c.advertiser_id.in_(ids[:limit]))
        .order_by(eligible.c.region_code)
    ).all()
    mapping: dict[str, list[str]] = {}
    for identity, code in rows:
        mapping.setdefault(identity, []).append(code)
    return Page(
        items=[
            TargetingAccount(
                advertiser_id=identity,
                region_codes=mapping.get(identity, []),
                reason_codes=[]
                if identity in mapping
                else ["scene_targeting_unavailable"],
            )
            for identity in ids[:limit]
        ],
        next_cursor=ids[limit - 1] if len(ids) > limit else None,
        total=total,
    )
