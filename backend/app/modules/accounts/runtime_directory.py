"""过期运行证据沿原冻结路由重新完整观察，不创建授权或改变默认连接。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import AuthorizationFacts
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.discovery import AUTHORIZED_LIST_SOURCE
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.accounts.capability_models import CapabilityJob
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.directory_merge import merge_directory_bc
from app.modules.accounts.mcp_discovery import (
    STAGES,
    _fresh,
    _indexed,
    _verified_rows,
    incomplete,
    staged_pages,
)
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.accounts.routing import verify_route
from app.modules.tenants.permissions import require_tenant

TASK_NAME = "accounts.runtime_discover"
MODE = "RUNTIME_REFRESH"


def current_authorization(
    session: Session, route: FrozenTikTokRoute
) -> ConnectionAuthorization | None:
    return session.exec(
        select(ConnectionAuthorization)
        .where(
            ConnectionAuthorization.tenant_id == route.tenant_id,
            ConnectionAuthorization.connection_id == route.connection_id,
            ConnectionAuthorization.authorization_revision
            == route.authorization_revision,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()


def authorization_basis(value: ConnectionAuthorization) -> str:
    # 固定原授权主体/范围；后续事实摘要收紧或时间变化不能暗中替换此原始依据。
    return sha256(
        json.dumps(
            [
                value.upstream_subject,
                value.upstream_grant_id,
                value.issuer,
                value.resource,
                sorted(value.scopes),
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def needs_directory_refresh(session: Session, route: FrozenTikTokRoute) -> bool:
    facts = current_authorization(session, route)
    if facts is None or facts.verified_at is None or not _fresh(facts.verified_at):
        return True
    if route.channel != "OFFICIAL_MCP":
        return False
    # 旧版本只发布读取事实。显式重检须补采上传授权，不能复用旧的未知结果；
    # GET 不入队，也不通过改写授权/绑定代数恢复历史任务。
    if (
        "mcp:tt4b" in facts.scopes
        and facts.permission_summary.get("upload_authorized") is None
    ):
        return True
    # 共享主体可由兄弟 BC 的同步续期，但资产/授权交集/详情必须是此 BC 的完整观察。
    # 角色重检会更新 grant.checked_at，不能拿该时间替代完整目录的完成证据。
    completed_at = session.exec(
        select(DiscoveryRun.completed_at)
        .where(
            DiscoveryRun.tenant_id == route.tenant_id,
            DiscoveryRun.connection_id == route.connection_id,
            DiscoveryRun.bc_id == route.bc_id,
            DiscoveryRun.authorization_revision == route.authorization_revision,
            DiscoveryRun.binding_revision == route.binding_revision,
            DiscoveryRun.status == "COMPLETE",
            col(DiscoveryRun.candidate_attempt_id).is_(None),
            col(DiscoveryRun.mcp_candidate_attempt_id).is_(None),
            col(DiscoveryRun.completed_at).is_not(None),
        )
        .order_by(col(DiscoveryRun.completed_at).desc())
        .limit(1)
    ).first()
    return completed_at is None or not _fresh(completed_at)


def queue_runtime(
    session: Session,
    run: DiscoveryRun,
    *,
    suffix: str = "step",
    due: datetime | None = None,
) -> None:
    identity = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="operator"
        ),
        task_name=TASK_NAME,
        task_key=f"runtime-directory:{run.id}:{run.revision}:{suffix}",
        payload={"run_id": str(run.id), "revision": run.revision},
    )
    if due is not None:
        dispatch = session.get(PendingDispatch, identity)
        assert dispatch is not None
        dispatch.available_at = due
        session.add(dispatch)


def ensure_runtime_directory(
    session: Session, *, job: CapabilityJob, context: TenantContext
) -> None:
    from app.modules.accounts.capabilities import _queue, _route

    route = _route(job)
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    original_authorization = current_authorization(session, route)
    if original_authorization is None:
        raise DomainError("capability_unavailable", "原连接缺少可比较的授权事实")
    # 调用者已持连接锁。MCP 每个 BC 可独立再观察，API 仍沿用整连接发现。
    active_query = select(DiscoveryRun).where(
        DiscoveryRun.tenant_id == route.tenant_id,
        DiscoveryRun.connection_id == route.connection_id,
        col(DiscoveryRun.status).in_(("RUNNING", "ADMISSION_WAIT")),
    )
    if route.channel == "OFFICIAL_MCP":
        active_query = active_query.where(DiscoveryRun.bc_id == route.bc_id)
    active = session.exec(active_query).one_or_none()
    if active is None:
        previous = session.exec(
            select(DiscoveryRun)
            .where(
                DiscoveryRun.tenant_id == route.tenant_id,
                DiscoveryRun.connection_id == route.connection_id,
                DiscoveryRun.work["capability_job_id"].as_string() == str(job.id),
                DiscoveryRun.work["mode"].as_string() == MODE,
            )
            .order_by(col(DiscoveryRun.created_at).desc())
        ).first()
        if previous is not None and previous.status in {"ERROR", "CANCELLED"}:
            raise DomainError(
                "capability_response_unverified", "完整授权目录再观察失败，请重新重检"
            )
        now = datetime.now(UTC)
        active = DiscoveryRun(
            tenant_id=job.tenant_id,
            actor_id=job.actor_id,
            connection_id=job.connection_id,
            credential_revision=job.credential_revision,
            bc_id=route.bc_id if route.channel == "OFFICIAL_MCP" else None,
            authorization_revision=route.authorization_revision,
            binding_revision=route.binding_revision,
            work={
                "mode": MODE,
                "route": route.model_dump(mode="json"),
                "capability_job_id": str(job.id),
                "directory_basis": job.directory_basis,
                "authorization_basis": authorization_basis(original_authorization),
                "stage": "SUBJECT",
                "page": 1,
                "bc_id": job.bc_id,
                "expires_at": (
                    now + timedelta(seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS)
                ).isoformat(),
                # 此摘要是冻结的适配合同标识，不冒充新的 tools/list 观察。
                "schema_digest": sha256(
                    route.adapter_contract_revision.encode()
                ).hexdigest(),
            },
        )
        session.add(active)
        session.flush()
        queue_runtime(session, active)
    # 重复派发等待同范围有界发现；不会重读默认连接或抢占兄弟 BC 的任务。
    job.revision += 1
    job.error_code = "route_evidence_stale"
    job.claim_token = job.claimed_until = None
    _queue(session, job, delay=60)


def locked_runtime_run(
    session: Session,
    *,
    context: TenantContext,
    run_id: UUID,
    claim_id: UUID | None = None,
    revision: int | None = None,
) -> tuple[DiscoveryRun, CapabilityJob, FrozenTikTokRoute]:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="build"
    )
    identity = session.get(DiscoveryRun, run_id, populate_existing=True)
    if identity is None or (identity.tenant_id, identity.actor_id) != (
        context.tenant_id,
        context.actor_id,
    ):
        raise DomainError("discovery_not_found", "当前租户运行目录任务不存在")
    session.exec(
        select(TikTokConnection)
        .where(TikTokConnection.id == identity.connection_id)
        .with_for_update()
    ).one()
    if identity.bc_id is not None:
        session.exec(
            select(BCConnectionBinding)
            .where(
                BCConnectionBinding.tenant_id == identity.tenant_id,
                BCConnectionBinding.connection_id == identity.connection_id,
                BCConnectionBinding.bc_id == identity.bc_id,
            )
            .with_for_update()
        ).one_or_none()
    run = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    try:
        if (
            run.work["mode"] != MODE
            or run.candidate_attempt_id
            or run.mcp_candidate_attempt_id
        ):
            raise ValueError("wrong mode")
        route = FrozenTikTokRoute.model_validate(run.work["route"])
        job_id = UUID(run.work["capability_job_id"])
        expires = datetime.fromisoformat(run.work["expires_at"])
    except KeyError, ValueError, TypeError:
        raise DomainError("discovery_stale", "运行目录缺少冻结依据") from None
    if (route.tenant_id, route.connection_id, route.bc_id) != (
        run.tenant_id,
        run.connection_id,
        run.work.get("bc_id"),
    ):
        raise DomainError("discovery_stale", "运行目录与原路由不匹配")
    if route.channel == "OFFICIAL_MCP" and (
        run.bc_id,
        run.authorization_revision,
        run.binding_revision,
    ) != (route.bc_id, route.authorization_revision, route.binding_revision):
        raise DomainError("discovery_stale", "MCP 运行目录与原绑定代数不匹配")
    job = session.exec(
        select(CapabilityJob)
        .where(CapabilityJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    from app.modules.accounts.capabilities import _directory_basis, _route

    if (
        job is None
        or (job.tenant_id, job.actor_id, job.connection_id, job.bc_id)
        != (run.tenant_id, run.actor_id, route.connection_id, route.bc_id)
        or _route(job) != route
    ):
        raise DomainError("discovery_stale", "运行目录与原能力任务不匹配")
    if run.status == "COMPLETE":
        if claim_id is not None:
            raise DomainError("discovery_stale", "此claim的完整目录已结束")
        return run, job, route
    if (
        run.status not in {"RUNNING", "ADMISSION_WAIT"}
        or job.status != "PENDING"
        or expires.tzinfo is None
        or expires <= datetime.now(UTC)
        or job.directory_basis != run.work.get("directory_basis")
        or _directory_basis(session, context, route.bc_id, route.connection_id)
        != job.directory_basis
        or (revision is not None and run.revision != revision)
        or (
            claim_id is not None
            and (
                run.claim_id != claim_id
                or run.claimed_until is None
                or run.claimed_until <= datetime.now(UTC)
            )
        )
    ):
        raise DomainError("discovery_stale", "运行目录已过期或原claim/目录已改变")
    verify_route(
        session, context=context, route=route, advertiser_id=None, capability="read"
    )
    current = current_authorization(session, route)
    if current is None or authorization_basis(current) != run.work.get(
        "authorization_basis"
    ):
        raise DomainError("route_authorization_changed", "原授权主体或范围已改变")
    return run, job, route


def _facts_from_stage(row: dict[str, Any]) -> AuthorizationFacts:
    try:
        if type(row["scopes"]) is not list:
            raise ValueError("scopes must be an observed list")
        return AuthorizationFacts(
            subject_id=row["subject_id"],
            grant_id=row["grant_id"],
            issuer=row["issuer"],
            resource=row["resource"],
            scopes=tuple(row["scopes"]),
            read_authorized=row["read_authorized"],
            upload_authorized=row["upload_authorized"],
            build_authorized=row["build_authorized"],
            evidence_source=row["evidence_source"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
        )
    except KeyError, TypeError, ValueError:
        raise incomplete() from None


def _same_authorization(
    observed: AuthorizationFacts, previous: ConnectionAuthorization
) -> bool:
    return (
        bool(observed.scopes)
        and _fresh(observed.observed_at)
        and observed.subject_id == previous.upstream_subject
        and observed.grant_id == previous.upstream_grant_id
        and observed.issuer == previous.issuer
        and observed.resource == previous.resource
        and sorted(observed.scopes) == sorted(previous.scopes)
    )


def publish_runtime_directory(
    session: Session,
    *,
    context: TenantContext,
    run_id: UUID,
    claim_id: UUID,
    revision: int,
) -> None:
    from app.modules.accounts.capabilities import _directory_basis, _queue, _scope_flags

    run, job, route = locked_runtime_run(
        session, context=context, run_id=run_id, claim_id=claim_id, revision=revision
    )
    if run.status == "COMPLETE":
        return
    if run.work.get("stage") != "FINALIZE":
        raise incomplete()
    schema_digest = sha256(route.adapter_contract_revision.encode()).hexdigest()
    all_pages = {
        stage: staged_pages(
            session,
            run_id=run.id,
            stage=stage,
            bc_id=route.bc_id if stage in {"ASSETS", "DETAILS", "ROLES"} else "",
        )
        for stage in STAGES
    }
    data = {
        stage: _verified_rows(
            pages,
            run=run,
            schema_digest=schema_digest,
            stage=stage,
            bc_id=route.bc_id if stage in {"ASSETS", "DETAILS", "ROLES"} else "",
            max_pages=2000
            if route.channel == "OFFICIAL_API" or stage == "AUTHORIZED"
            else 1000,
        )
        for stage, pages in all_pages.items()
    }
    # 能力角色证据沿用原重检合同：必须有远端精确总数，不能把本地累加冒充total。
    if all_pages["ROLES"][0].total_number is None:
        raise incomplete()
    if len(data["SUBJECT"]) != 1:
        raise incomplete()
    observed = _facts_from_stage(data["SUBJECT"][0])
    previous = current_authorization(session, route)
    if previous is None or not _same_authorization(observed, previous):
        raise DomainError(
            "route_authorization_changed", "再观察的主体或授权范围与原路由不同"
        )
    if route.channel == "OFFICIAL_MCP":
        if (
            observed.evidence_source != "MCP_USER_INFO_AND_TOKEN"
            or not observed.subject_id
            or "mcp:tt4b" not in observed.scopes
        ):
            raise incomplete()
    elif observed.evidence_source != "OFFICIAL_TOKEN_SCOPE":
        raise incomplete()
    authorized = _indexed(data["AUTHORIZED"], key="advertiser_id")
    bcs = _indexed(data["BCS"], key="bc_id")
    assets = _indexed(data["ASSETS"], key="advertiser_id")
    details = _indexed(data["DETAILS"], key="advertiser_id")
    roles = _indexed(data["ROLES"], key="advertiser_id")
    if (
        route.bc_id not in bcs
        or set(details) != set(assets) & set(authorized)
        or set(roles) != set(assets)
    ):
        raise incomplete()
    if any(
        p.call_evidence.get("completeness_source") != AUTHORIZED_LIST_SOURCE
        for p in all_pages["AUTHORIZED"]
    ):
        raise incomplete()
    if len(all_pages["ASSETS"]) != len(all_pages["DETAILS"]):
        raise incomplete()
    for assets_page, details_page in zip(
        all_pages["ASSETS"], all_pages["DETAILS"], strict=True
    ):
        if {r["advertiser_id"] for r in details_page.rows} != {
            r["advertiser_id"] for r in assets_page.rows
        } & set(authorized):
            raise incomplete()
    if any(
        r.get("role") not in {None, "ADMIN", "OPERATOR", "ANALYST"}
        for r in roles.values()
    ) or any(
        type(row.get(key)) is not str
        for row in details.values()
        for key in ("name", "currency", "timezone", "remote_status")
    ):
        raise incomplete()
    # 只有完整阶段与原授权语义CAS均成功，才原子替换所选BC。其他BC绑定/默认不变。
    merge_directory_bc(
        session,
        run=run,
        bc_id=route.bc_id,
        bc_name=str(bcs[route.bc_id].get("name", "")),
    )
    params = {
        "tenant_id": run.tenant_id,
        "connection_id": run.connection_id,
        "bc_id": route.bc_id,
        "run_id": run.id,
        "job_id": job.id,
        "restrict_bc": route.channel == "OFFICIAL_MCP",
    }
    SASession.execute(
        session,
        text("""UPDATE bc_account_access SET in_bc=false,authorized=false,active=false,can_upload=false,can_build=false
        WHERE tenant_id=:tenant_id AND bc_id=:bc_id AND connection_id=:connection_id AND last_seen_run_id IS DISTINCT FROM :run_id"""),
        params,
    )
    # API 保留完整授权集合的全连接收紧规则；MCP 每个 BC 独立发布，不改兄弟 BC 证据。
    SASession.execute(
        session,
        text("""WITH authorized AS (
            SELECT item->>'advertiser_id' AS id FROM discovery_staged_page p
            CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
            WHERE p.run_id=:run_id AND p.stage='AUTHORIZED' AND p.bc_id=''
        )
        UPDATE bc_account_access g
        SET authorized=false,active=false,can_upload=false,can_build=false
        WHERE g.tenant_id=:tenant_id AND g.connection_id=:connection_id
        AND (NOT :restrict_bc OR g.bc_id=:bc_id)
        AND NOT EXISTS (SELECT 1 FROM authorized a WHERE a.id=g.advertiser_id)
        AND (g.authorized OR g.active OR g.can_upload OR g.can_build)"""),
        params,
    )
    previous.verified_at = observed.observed_at
    previous.permission_summary = (
        {
            "read_authorized": True,
            "upload_authorized": observed.upload_authorized,
            "build_authorized": observed.build_authorized,
        }
        if route.channel == "OFFICIAL_API"
        else {
            **previous.permission_summary,
            "read_authorized": True,
            "upload_authorized": observed.upload_authorized,
        }
    )
    previous.source = (
        "OFFICIAL_TOKEN_AND_COMPLETE_DIRECTORY"
        if route.channel == "OFFICIAL_API"
        else "MCP_COMPLETE_DIRECTORY_READ"
    )
    session.add(previous)
    # 角色页也是此次完整观察的一部分，原job接着发布这些事实，不再重复读取或另建任务。
    SASession.execute(
        session,
        text("DELETE FROM account_capability_asset WHERE job_id=:job_id"),
        params,
    )
    SASession.execute(
        session,
        text("DELETE FROM account_capability_page WHERE job_id=:job_id"),
        params,
    )
    SASession.execute(
        session,
        text("""INSERT INTO account_capability_page (job_id,page,tenant_id,bc_id,row_count,observed_at)
        SELECT :job_id,page,tenant_id,bc_id,jsonb_array_length(rows),observed_at FROM discovery_staged_page
        WHERE run_id=:run_id AND bc_id=:bc_id AND stage='ROLES'"""),
        params,
    )
    SASession.execute(
        session,
        text("""INSERT INTO account_capability_asset (job_id,advertiser_id,tenant_id,bc_id,page,role)
        SELECT :job_id,item->>'advertiser_id',p.tenant_id,p.bc_id,p.page,item->>'role'
        FROM discovery_staged_page p CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE run_id=:run_id AND bc_id=:bc_id AND stage='ROLES' AND item->>'role' IS NOT NULL"""),
        params,
    )
    session.flush()
    basis = _directory_basis(session, context, route.bc_id, route.connection_id)
    if basis is None:
        raise incomplete()
    job.directory_basis = basis
    job.phase = "PUBLISH"
    job.total_pages = len(all_pages["ROLES"])
    job.total_count = job.seen_count = len(roles)
    job.next_page = job.total_pages + 1
    job.publish_after = None
    job.published_count = 0
    job.expires_at = observed.observed_at + timedelta(
        seconds=settings.BC_CAPABILITY_MAX_AGE_SECONDS
    )
    job.scope_known, job.scope_build, job.scope_upload = _scope_flags(observed)
    job.claim_token = job.claimed_until = None
    job.error_code = None
    job.revision += 1
    _queue(session, job)
    run.status = "COMPLETE"
    run.completed_at = datetime.now(UTC)
    run.error_code = None
    run.claim_id = run.claimed_until = None
    session.add_all([run, job])
    # 活动job唯一约束冲突会回滚目录、facts及job全部改动；调用者记录明确stale。
    session.flush()
