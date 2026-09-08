"""Pure preview reads and separate, one-GET, fenced scene refresh units."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from celery import current_task  # type: ignore[import-untyped]
from sqlalchemy.dialects.postgresql import array, insert
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import admitted_account_call, sdk_client
from app.jobs.admission import admission_policy
from app.modules.accounts.access import resolve_account_access, usable_grants
from app.modules.accounts.models import BCAccountAccess, TikTokConnection
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
)
from app.modules.tenants.permissions import require_tenant

from . import scene_constraints as limits
from . import scene_sdk as api
from .scene_models import SceneEvidence, SceneReadState
from .scene_schemas import SceneContext, SceneRefreshResult, SceneResource

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
    limit = (
        (task.request.timelimit or (None, None))[0] or getattr(task, "time_limit", None)
        if task
        else None
    )
    if (
        not task
        or not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or task.request.called_directly
        or task.request.is_eager
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not 0 < limit <= HARD_LIMIT
    ):
        raise DomainError("scene_worker_unbounded", "场景刷新需要有界后台任务")


def _scope(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
    lock: bool = False,
) -> dict[str, Any]:
    access = resolve_account_access(
        session,
        context=context,
        bc_id=bc_id,
        advertiser_id=advertiser_id,
        action="read",
    )
    statement = select(TikTokConnection).where(
        TikTokConnection.id == access.connection_id,
        TikTokConnection.tenant_id == context.tenant_id,
    )
    if lock:
        statement = statement.with_for_update()
    connection = session.exec(statement.execution_options(populate_existing=True)).one()
    link = session.exec(
        select(PromotionLink)
        .where(
            PromotionLink.id == link_id, PromotionLink.tenant_id == context.tenant_id
        )
        .execution_options(populate_existing=True)
    ).first()
    if link is None:
        raise DomainError("resource_not_found", "当前租户推广链接不存在")
    provider = session.get(
        ProviderConnection, link.connection_id, populate_existing=True
    )
    app = session.exec(
        select(ProviderApplication)
        .where(
            ProviderApplication.tenant_id == context.tenant_id,
            ProviderApplication.connection_id == link.connection_id,
            ProviderApplication.external_id == link.application_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    if (
        not provider
        or provider.tenant_id != context.tenant_id
        or provider.status != "active"
        or not provider.verification_token
        or app.channel_config.get("verification_token")
        != str(provider.verification_token)
        or link.status != "ready"
        or not link.verified_at
    ):
        raise DomainError("scene_link_unavailable", "推广链接或版权方应用需要重新核实")
    grant = session.get(
        BCAccountAccess,
        (context.tenant_id, bc_id, advertiser_id, access.connection_id),
        populate_existing=True,
    )
    assert grant is not None
    basis = {
        "sdk_contract_revision": api.CONTRACT_REVISION,
        "connection_id": str(connection.id),
        "credential_version": connection.credential_version,
        "grant_run": str(grant.last_seen_run_id),
        "provider_version": provider.credential_version,
        "provider_verification": str(provider.verification_token),
        "link_version": link.version,
        "link_url": link.url,
        "minis_id": app.tiktok_minis_id,
    }
    return {
        "access": access,
        "connection": connection,
        "grant": grant,
        "minis_id": app.tiktok_minis_id,
        "basis": sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest(),
    }


def _states(
    context: TenantContext, bc_id: str, advertiser_id: str, link_id: UUID
) -> Any:
    return select(SceneReadState).where(
        SceneReadState.tenant_id == context.tenant_id,
        SceneReadState.bc_id == bc_id,
        SceneReadState.advertiser_id == advertiser_id,
        SceneReadState.link_id == link_id,
    )


def _scope_capabilities(connection: TikTokConnection) -> tuple[bool, bool, bool]:
    private = decrypt_credentials(
        tenant_id=connection.tenant_id,
        ciphertext=connection.credential_ciphertext or "",
    )
    try:
        values = json.loads(private["scope"])
        if (
            not isinstance(values, list)
            or len(values) > 1024
            or any(type(x) is not int or not 0 < x < 2**64 for x in values)
        ):
            return False, False, False
    except KeyError, ValueError, TypeError:
        return False, False, False
    scope = set(values)
    # Parent 2 covers Ads Management; parent 6 / Video Management / upload leaf
    # are explicitly documented hierarchical capabilities. No visibility inference.
    return True, 2 in scope, bool(scope & {6, 61, 611})


def _merge(
    previous: dict[str, Any],
    page: dict[str, Any],
    *,
    resource: SceneResource,
    first: bool,
    last: bool,
) -> dict[str, Any]:
    if resource in {"cta", "vbo"}:
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


def refresh_scene_context(
    *,
    database_engine: Any,
    redis_client: Any,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
    resource: SceneResource,
    previous_evidence_id: UUID | None = None,
) -> SceneRefreshResult:
    """One page per call. Caller schedules continuation using returned evidence ID.

    Production callers must be prefork tasks with time_limit<=45. There is no
    automatic network retry and no remote mutation. All DB sessions close before I/O.
    """
    _require_bounded_worker()
    if resource not in RESOURCES:
        raise DomainError("scene_request_invalid", "场景读取参数无效")
    policy = admission_policy(api.ENDPOINTS[resource])
    if policy.lease_ms <= (HARD_LIMIT + 5) * 1000:
        raise DomainError("admission_policy_invalid", "场景调用租约短于工作进程硬限")
    now, token = datetime.now(UTC), uuid4()
    with Session(database_engine) as session, session.begin():
        require_tenant(
            session,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
            action="build",
        )
        scope = _scope(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            link_id=link_id,
            lock=True,
        )
        values = {
            "id": uuid4(),
            "tenant_id": context.tenant_id,
            "bc_id": bc_id,
            "advertiser_id": advertiser_id,
            "link_id": link_id,
            "connection_id": scope["connection"].id,
            "resource": resource,
            "basis_digest": scope["basis"],
            "generation": uuid4(),
            "next_page": 1,
            "complete": False,
            "facts": {},
        }
        session.execute(  # ty: ignore[deprecated] -- PostgreSQL INSERT, not ORM SELECT
            insert(SceneReadState)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_scene_state_scope")
        )
        state = session.exec(
            _states(context, bc_id, advertiser_id, link_id)
            .where(SceneReadState.resource == resource)
            .with_for_update()
        ).one()
        if state.claimed_until and state.claimed_until > now:
            return SceneRefreshResult(
                state.last_evidence_id,
                resource,
                False,
                None,
                ("scene_refresh_in_progress",),
            )
        if previous_evidence_id is not None:
            if (
                state.last_evidence_id != previous_evidence_id
                or state.complete
                or state.basis_digest != scope["basis"]
                or not state.expires_at
                or state.expires_at <= now
            ):
                raise DomainError("scene_refresh_stale", "场景分页已失效，需要重新刷新")
        else:
            state.generation, state.next_page, state.facts = uuid4(), 1, {}
            state.last_evidence_id, state.complete = None, False
            state.expires_at = now + timedelta(seconds=FRESH_SECONDS)
        state.connection_id, state.basis_digest = scope["connection"].id, scope["basis"]
        state.attempt_token, state.claimed_until, state.error_code = (
            token,
            now + timedelta(seconds=CLAIM_SECONDS),
            None,
        )
        state_id, generation, page, basis, connection_id = (
            state.id,
            state.generation,
            state.next_page,
            state.basis_digest,
            state.connection_id,
        )
        minis_id, previous, expires = (
            scope["minis_id"],
            dict(state.facts),
            state.expires_at,
        )
    try:
        with admitted_account_call(
            redis_client,
            context=context,
            endpoint=api.ENDPOINTS[resource],
            advertiser_id=advertiser_id,
            policy=policy,
        ):
            with Session(database_engine) as session:
                fresh = _scope(
                    session,
                    context=context,
                    bc_id=bc_id,
                    advertiser_id=advertiser_id,
                    link_id=link_id,
                )
                if fresh["basis"] != basis:
                    raise DomainError("scene_refresh_stale", "场景授权已更新")
                require_tenant(
                    session,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    action="build",
                )
                with sdk_client(
                    session, context=context, connection_id=connection_id
                ) as client:
                    session.close()
                    response = api.request_page(
                        client,
                        resource=resource,
                        bc_id=bc_id,
                        advertiser_id=advertiser_id,
                        page=page,
                    )
        facts, last, request_id = api.parse_page(
            response,
            resource=resource,
            page=page,
            advertiser_id=advertiser_id,
            bc_id=bc_id,
            minis_id=minis_id,
        )
        combined = _merge(
            previous, facts, resource=resource, first=page == 1, last=last
        )
        with Session(database_engine) as session, session.begin():
            require_tenant(
                session,
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                action="build",
            )
            fresh = _scope(
                session,
                context=context,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                link_id=link_id,
                lock=True,
            )
            state = session.exec(
                select(SceneReadState)
                .where(
                    SceneReadState.id == state_id,
                    SceneReadState.tenant_id == context.tenant_id,
                )
                .with_for_update()
            ).one()
            observed = datetime.now(UTC)
            if (
                state.attempt_token != token
                or state.generation != generation
                or state.claimed_until is None
                or state.claimed_until <= observed
                or fresh["basis"] != basis
                or expires is None
                or expires <= observed
            ):
                return SceneRefreshResult(
                    None, resource, False, None, ("scene_refresh_stale",)
                )
            if page > 1 and facts.get("item_id_hashes"):
                repeated = session.exec(
                    select(SceneEvidence.id)
                    .where(
                        SceneEvidence.tenant_id == context.tenant_id,
                        SceneEvidence.state_id == state_id,
                        SceneEvidence.generation == generation,
                        SceneEvidence.endpoint == api.ENDPOINTS[resource],
                        SceneEvidence.page < page,
                        col(SceneEvidence.facts)["item_id_hashes"].op("?|")(
                            array(facts["item_id_hashes"])
                        ),
                    )
                    .limit(1)
                ).first()
                if repeated is not None:
                    raise DomainError(
                        "scene_pagination_changed", "远端列表包含重复记录，需要重新刷新"
                    )
            if resource == "account_roles" and last:
                known, build, upload = _scope_capabilities(fresh["connection"])
                matches = combined.get("matches", [])
                role = matches[0]["role"] if len(matches) == 1 else None
                can_operate = role in {"ADMIN", "OPERATOR"}
                grant = fresh["grant"]
                grant.permission_state = "VERIFIED" if known and role else "UNKNOWN"
                grant.can_build, grant.can_upload = (
                    known and build and can_operate,
                    known and upload and can_operate,
                )
                grant.checked_at = observed
                combined.update(
                    scope_verified=known,
                    can_build=grant.can_build,
                    can_upload=grant.can_upload,
                )
            evidence = SceneEvidence(
                tenant_id=context.tenant_id,
                state_id=state_id,
                generation=generation,
                page=page,
                endpoint=api.ENDPOINTS[resource],
                request_id=request_id,
                source_revision=api.CONTRACT_REVISION,
                basis_digest=basis,
                facts=facts,
                observed_at=observed,
                expires_at=expires,
            )
            session.add(evidence)
            session.flush()
            state.facts, state.complete, state.last_evidence_id = (
                combined,
                last,
                evidence.id,
            )
            state.next_page = page + 1
            state.attempt_token, state.claimed_until = None, None
            return SceneRefreshResult(
                evidence.id, resource, last, None if last else page + 1
            )
    except Exception as error:
        code = error.code if isinstance(error, DomainError) else "scene_refresh_failed"
        # Never persist a raw SDK message, URL, token, scope receipt or exception.
        safe = (
            code
            if code
            in {
                "scene_response_unverified",
                "scene_pagination_changed",
                "scene_refresh_stale",
                "action_forbidden",
                "tenant_forbidden",
                "admission_deferred",
                "admission_unavailable",
            }
            else "scene_refresh_failed"
        )
        with Session(database_engine) as session, session.begin():
            state = session.exec(
                select(SceneReadState)
                .where(
                    SceneReadState.id == state_id,
                    SceneReadState.tenant_id == context.tenant_id,
                )
                .with_for_update()
            ).one()
            if state.attempt_token == token:
                (
                    state.attempt_token,
                    state.claimed_until,
                    state.complete,
                    state.error_code,
                ) = None, None, False, safe
        return SceneRefreshResult(None, resource, False, None, (safe,))


def read_scene_context(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    link_id: UUID,
) -> SceneContext:
    """Local facts only. No network, credential decryption, mutation or flush."""
    with session.no_autoflush:
        scope = _scope(
            session,
            context=context,
            bc_id=bc_id,
            advertiser_id=advertiser_id,
            link_id=link_id,
        )
        states = session.exec(
            _states(context, bc_id, advertiser_id, link_id).execution_options(
                populate_existing=True
            )
        ).all()
    now = datetime.now(UTC)
    available = {
        state.resource: state
        for state in states
        if state.basis_digest == scope["basis"]
        and state.complete
        and state.expires_at
        and state.expires_at > now
    }
    reasons = []
    if len(available) != len(RESOURCES):
        reasons.append("scene_evidence_missing")
    if any(state.expires_at and state.expires_at <= now for state in states):
        reasons.append("scene_evidence_expired")
    reasons.extend(state.error_code for state in states if state.error_code)
    grant = scope["grant"]
    with session.no_autoflush:
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
    roles = available.get("account_roles")
    if not roles or not roles.facts.get("scope_verified"):
        reasons.append("account_scope_unverified")
    if (
        not locally_operable
        or not roles
        or not roles.facts.get("can_build")
        or grant.permission_state != "VERIFIED"
        or not grant.can_build
    ):
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
    constraints, missing_constraints = limits.constraints_for(scope["access"].currency)
    reasons.extend(missing_constraints)
    minis = available.get("minis")
    matches = minis.facts.get("matches", []) if minis else []
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
        }
        constraints["allowed_region_codes"] = matches[0]["regions"]
    else:
        reasons.append("minis_unavailable")
    identity = available.get("identity")
    identities = identity.facts.get("matches", []) if identity else []
    if len(identities) == 1:
        creative = {"creative_info": {**identities[0], "ad_format": "SINGLE_VIDEO"}}
    else:
        reasons.append(
            "identity_selection_required" if identities else "identity_unavailable"
        )
    dynamic = available.get("cta")
    if (
        dynamic
        and dynamic.facts.get("asset_ids")
        and dynamic.facts.get("recommend_assets")
    ):
        cta = {
            "asset_ids": dynamic.facts["asset_ids"],
            "recommend_assets": dynamic.facts["recommend_assets"],
            "requires_portfolio_creation": True,
        }
    else:
        reasons.append("cta_unavailable")
    vbo = available.get("vbo")
    if not vbo or vbo.facts.get("vo_min_roas") != "QUALIFIED":
        reasons.append("minis_vbo_unverified")
    evidence_ids = tuple(
        state.last_evidence_id
        for state in states
        if state.last_evidence_id and state.basis_digest == scope["basis"]
    )
    revision = sha256(
        (
            api.CONTRACT_REVISION
            + limits.REVISION
            + scope["basis"]
            + "".join(sorted(str(value) for value in evidence_ids))
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
        copy_length_limit=limits.COPY_LENGTH_LIMIT or 0,
        evidence_ids=evidence_ids,
        field_constraints=constraints,
    )
