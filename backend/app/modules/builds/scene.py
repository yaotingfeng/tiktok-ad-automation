"""预览仅读持久事实；场景准备通过固定路由的有界后台调用完成。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.accounts.routing import verify_route
from app.modules.accounts.schemas import AccountAccess
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
)
from app.modules.tenants.permissions import require_tenant

from . import scene_constraints as limits
from .scene_models import SceneReadState
from .scene_schemas import SceneContext, SceneResource

HARD_LIMIT = 45
CLAIM_SECONDS = 60
FRESH_SECONDS = 600  # local evidence freshness policy, not a TikTok limit
RESOURCES: tuple[SceneResource, ...] = (
    "account_roles",
    "identity",
    "minis",
    "cta",
    "vbo",
)


def _require_bounded_worker() -> None:
    task = current_task
    request = getattr(task, "request", None)
    limits = getattr(request, "timelimit", None)
    limit = limits[0] if isinstance(limits, (tuple, list)) and limits else None
    if limit is None:
        limit = getattr(task, "time_limit", None)
    if (
        not request
        or not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or getattr(request, "called_directly", True)
        or getattr(request, "is_eager", True)
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not 0 < limit <= HARD_LIMIT
    ):
        raise DomainError("scene_worker_unbounded", "场景刷新需要有界后台任务")


SCENE_CONTRACT_REVISION = "dual-channel-scene-2026-09-12-v1"


def scene_scope_basis(*, route: FrozenTikTokRoute, business: dict[str, Any]) -> str:
    """只散列冻结授权与稳定业务依据；令牌字节/目录run/观察时间不属于输入。"""
    route_basis = route.model_dump(mode="json")
    # 旧六字段冻结路由等价于绑定代数 0；保持其历史摘要，避免令已有场景无故失效。
    # 这里只规范化哈希输入，不回写历史 JSON；重新接入后的非零代数必须参与摘要。
    if route.binding_revision == 0:
        route_basis.pop("binding_revision")
    basis = {
        "route": route_basis,
        "scene_contract_revision": SCENE_CONTRACT_REVISION,
        "constraints_revision": limits.REVISION,
        "max_age_seconds": settings.SCENE_MAX_AGE_SECONDS,
        "business": business,
    }
    return sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _application_scope(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    provider_connection_id: UUID,
    application_id: str,
    route: FrozenTikTokRoute,
    lock: bool = False,
) -> dict[str, Any]:
    if route.tenant_id != context.tenant_id or route.bc_id != bc_id:
        raise DomainError("connection_bc_mismatch", "场景路由不属于当前租户或 BC")
    chosen = route.connection_id
    # 场景可以在写能力重检前准备，但读取也必须有原连接的当前明确授权。
    verify_route(
        session,
        context=context,
        route=route,
        advertiser_id=advertiser_id,
        capability="read",
    )
    access = resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        action="read",
        connection_id=chosen,
    )
    grant = session.exec(
        usable_grants(tenant_id=context.tenant_id, bc_id=bc_id, action="read")
        .where(
            BCAccountAccess.advertiser_id == advertiser_id,
            BCAccountAccess.connection_id == chosen,
        )
        .execution_options(populate_existing=True)
    ).first()
    if grant is None:
        raise DomainError("account_access_denied", "当前授权不支持该账户操作")
    statement = select(TikTokConnection).where(
        TikTokConnection.id == chosen, TikTokConnection.tenant_id == context.tenant_id
    )
    if lock:
        statement = statement.with_for_update()
    connection = session.exec(statement.execution_options(populate_existing=True)).one()
    if lock:
        verify_route(
            session,
            context=context,
            route=route,
            advertiser_id=advertiser_id,
            capability="read",
        )
    provider = session.get(
        ProviderConnection, provider_connection_id, populate_existing=True
    )
    app = session.exec(
        select(ProviderApplication)
        .where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == provider_connection_id,
            ProviderApplication.external_id == application_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        not provider
        or provider.tenant_id != context.tenant_id
        or provider.status != "active"
        or not provider.verification_token
        or app is None
        or app.channel_config.get("verification_token")
        != str(provider.verification_token)
    ):
        raise DomainError("scene_link_unavailable", "版权方应用需要重新核实")
    business = {
        "advertiser_id": advertiser_id,
        "currency": access.currency,
        "timezone": access.timezone,
        "provider_id": str(provider.id),
        "provider_version": provider.credential_version,
        "provider_verification": str(provider.verification_token),
        "application_id": app.external_id,
        "minis_id": app.tiktok_minis_id,
    }
    return {
        "access": AccountAccess(
            advertiser_id=advertiser_id,
            bc_id=bc_id,
            connection_id=chosen,
            currency=access.currency,
            timezone=access.timezone,
        ),
        "connection": connection,
        "grant": grant,
        "provider": provider,
        "application": app,
        "minis_id": app.tiktok_minis_id,
        "route": route,
        "basis": scene_scope_basis(route=route, business=business),
    }


def _scope(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
    route: FrozenTikTokRoute,
    lock: bool = False,
) -> dict[str, Any]:
    # Link checks happen on every consumer request; its URL/version are not input
    # parameters to the reusable account/application GETs.
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    link = session.exec(
        select(PromotionLink)
        .where(
            PromotionLink.id == link_id,
            PromotionLink.tenant_id == context.tenant_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if link is None:
        raise DomainError("resource_not_found", "当前租户推广链接不存在")
    if link.status != "ready" or not link.verified_at:
        raise DomainError("scene_link_unavailable", "推广链接需要重新核实")
    return _application_scope(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        provider_connection_id=link.connection_id,
        application_id=link.application_id,
        route=route,
        lock=lock,
    )


def _states(
    context: TenantContext, bc_id: str, advertiser_id: str, link_id: UUID
) -> Any:
    return select(SceneReadState).where(
        SceneReadState.tenant_id == context.tenant_id,
        SceneReadState.bc_id == bc_id,
        SceneReadState.advertiser_id == advertiser_id,
        SceneReadState.link_id == link_id,
    )


def _merge(
    previous: dict[str, Any],
    page: dict[str, Any],
    *,
    resource: SceneResource,
    first: bool,
    last: bool,
) -> dict[str, Any]:
    if resource in {"cta", "vbo", "regions"}:
        return page
    if not first and (
        previous.get("total_number") != page["total_number"]
        or previous.get("total_page") != page["total_page"]
    ):
        raise DomainError("scene_pagination_changed", "远端列表已变化，需要重新刷新")
    seen = (0 if first else previous.get("seen", 0)) + page["seen"]
    if last and seen != page["total_number"]:
        raise DomainError("scene_response_unverified", "远端列表缺少完整分页证据")
    matches = ([] if first else previous.get("matches", [])) + page["matches"]
    # Full-list uniqueness proof stays in bounded per-page evidence, not this
    # compact accumulator or public SceneContext.
    compact = {key: value for key, value in page.items() if key != "item_id_hashes"}
    return {**compact, "seen": seen, "matches": matches[:2]}


def _assemble_scene(
    *,
    scope: dict[str, Any],
    facts: dict[str, Any],
    reasons: list[str],
    evidence_ids: tuple[UUID, ...],
    capability: Any,
    locally_operable: bool,
) -> SceneContext:
    if capability is None or not capability.scope_verified:
        reasons.append("account_scope_unverified")
    if capability is None or not capability.can_build or not locally_operable:
        reasons.append("account_build_unverified")
    campaign = {
        "objective_type": "APP_PROMOTION",
        "app_promotion_type": "MINIS",
        "campaign_type": "REGULAR_CAMPAIGN",
        "catalog_enabled": False,
        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
    }
    group: dict[str, Any] = {}
    creative: dict[str, Any] = {}
    cta: dict[str, Any] = {}
    constraints, missing = limits.constraints_for(scope["access"].currency)
    reasons.extend(missing)
    matches = facts.get("minis", {}).get("matches", [])
    if (
        len(matches) == 1
        and matches[0]["status"] == "ACTIVE"
        and matches[0]["type"] == "MINI_SERIES"
    ):
        group = {
            "promotion_type": "MINI_APP",
            "minis_id": matches[0]["minis_id"],
            "optimization_goal": "VALUE",
            "optimization_event": "ACTIVE_PAY",
            "bid_type": "BID_TYPE_NO_BID",
            "deep_bid_type": "VO_MIN_ROAS",
            "billing_event": "OCPM",
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
        }
        allowed = matches[0]["regions"]
        constraints["allowed_region_codes"] = allowed
        targets = [
            item
            for item in facts.get("regions", {}).get("locations", [])
            if item["region_code"] in allowed
        ]
        if targets:
            group["targeting_spec"] = {
                "location_ids": sorted(item["location_id"] for item in targets)
            }
            constraints["target_regions"] = targets
            constraints["targeting_source"] = "tool-region-doc1737189539571713"
        else:
            reasons.append("scene_targeting_unavailable")
    else:
        reasons.append("minis_unavailable")
    identities = facts.get("identity", {}).get("matches", [])
    if len(identities) == 1:
        creative = {"creative_info": {**identities[0], "ad_format": "SINGLE_VIDEO"}}
    else:
        reasons.append(
            "identity_selection_required" if identities else "identity_unavailable"
        )
    dynamic = facts.get("cta", {})
    if dynamic.get("asset_ids") and dynamic.get("recommend_assets"):
        cta = {
            "asset_ids": dynamic["asset_ids"],
            "recommend_assets": dynamic["recommend_assets"],
            "requires_portfolio_creation": True,
        }
    else:
        reasons.append("cta_unavailable")
    if facts.get("vbo", {}).get("vo_min_roas") != "QUALIFIED":
        reasons.append("minis_vbo_unverified")
    revision = sha256(
        (
            scope["basis"] + "".join(sorted(str(value) for value in evidence_ids))
        ).encode()
    ).hexdigest()
    return SceneContext(
        supported=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        capability_revision=revision,
        campaign_fields=campaign,
        adgroup_fields=group,
        creative_fields=creative,
        cta_fields=cta,
        name_limit=512,
        creative_limit=50,
        copy_length_limit=limits.COPY_LENGTH_LIMIT,
        evidence_ids=evidence_ids,
        field_constraints=constraints,
    )


def read_scene_context(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
    route: FrozenTikTokRoute,
) -> SceneContext:
    """Pure local facts. Link remains validated even when assets are shared."""
    from app.modules.accounts.capabilities import get_capability_evidence

    from .scene_job_models import SceneJob

    with session.no_autoflush:
        scope = _scope(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            link_id=link_id,
            route=route,
        )
        now = datetime.now(UTC)
        job = session.exec(
            select(SceneJob)
            .where(
                SceneJob.tenant_id == context.tenant_id,
                SceneJob.scope_basis == scope["basis"],
            )
            .order_by(col(SceneJob.created_at).desc(), col(SceneJob.id).desc())
            .limit(1)
            .execution_options(populate_existing=True)
        ).first()
        reasons: list[str] = []
        facts: dict[str, Any] = {}
        evidence_ids: tuple[UUID, ...] = ()
        if job is not None:
            if job.status == "COMPLETE" and job.expires_at and job.expires_at > now:
                facts, evidence_ids = job.facts, (job.id,)
            else:
                reasons.append(
                    "scene_evidence_expired"
                    if job.expires_at and job.expires_at <= now
                    else "scene_evidence_missing"
                )
                if job.error_code:
                    reasons.append(job.error_code)
        else:
            # Legacy direct refresh remains readable for diagnostics, but cannot
            # establish current build permission without the shared BC proof.
            states = session.exec(
                _states(context, bc_id, advertiser_id, link_id).execution_options(
                    populate_existing=True
                )
            ).all()
            facts = {
                state.resource: state.facts
                for state in states
                if state.basis_digest == scope["basis"]
                and state.complete
                and state.expires_at
                and state.expires_at > now
            }
            evidence_ids = tuple(
                state.last_evidence_id
                for state in states
                if state.last_evidence_id and state.basis_digest == scope["basis"]
            )
            reasons.append("scene_evidence_missing")
            if any(state.expires_at and state.expires_at <= now for state in states):
                reasons.append("scene_evidence_expired")
            reasons.extend(state.error_code for state in states if state.error_code)
        capability = get_capability_evidence(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            connection_id=scope["connection"].id,
        )
        if capability:
            evidence_ids += capability.evidence_ids
        try:
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="build",
            )
            locally_operable = (
                session.exec(
                    usable_grants(
                        tenant_id=context.tenant_id, bc_id=bc_id, action="build"
                    ).where(
                        BCAccountAccess.advertiser_id == advertiser_id,
                        BCAccountAccess.connection_id == scope["connection"].id,
                    )
                ).first()
                is not None
            )
        except DomainError:
            locally_operable = False
        return _assemble_scene(
            scope=scope,
            facts=facts,
            reasons=reasons,
            evidence_ids=evidence_ids,
            capability=capability,
            locally_operable=locally_operable,
        )
