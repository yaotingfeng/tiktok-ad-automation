"""MCP 每个 BC 独立暂存与原子发布；失败保留该 BC 的旧快照。"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import AuthorizationFacts
from app.integrations.tiktok.contracts.discovery import AUTHORIZED_LIST_SOURCE
from app.integrations.tiktok.mcp.protocol import (
    load_mcp_protocol,
    load_tool_contracts,
    verify_tool_schema,
)
from app.integrations.tiktok.mcp_auth.service import require_mcp_admin
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
    ConnectionToolObservation,
)
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.models import DiscoveryRun, TikTokConnection
from app.modules.tenants.models import AuditEvent

PAGE_SIZE = 50
STAGES = ("SUBJECT", "AUTHORIZED", "BCS", "ASSETS", "DETAILS", "ROLES")


def incomplete() -> DomainError:
    return DomainError("discovery_incomplete", "BC 目录证据尚未完整核实")


def _fresh(observed_at: datetime) -> bool:
    age = (datetime.now(UTC) - observed_at).total_seconds()
    return 0 <= age <= settings.BC_CAPABILITY_MAX_AGE_SECONDS


def locked_mcp_run(
    session: Session, *, context: TenantContext, run_id: UUID, check_admin: bool = True
) -> tuple[DiscoveryRun, TikTokConnection]:
    if check_admin:
        require_mcp_admin(
            session, actor_id=context.actor_id, tenant_id=context.tenant_id
        )
    run = session.get(DiscoveryRun, run_id, populate_existing=True)
    if (
        run is None
        or run.tenant_id != context.tenant_id
        or run.actor_id != context.actor_id
    ):
        raise DomainError("discovery_not_found", "当前租户发现任务不存在")
    # 与绑定、解绑和刷新统一锁序；令牌轮换不改变授权或绑定代数。
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == run.connection_id,
            TikTokConnection.tenant_id == context.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    binding = session.exec(
        select(BCConnectionBinding)
        .where(
            BCConnectionBinding.tenant_id == run.tenant_id,
            BCConnectionBinding.connection_id == run.connection_id,
            BCConnectionBinding.bc_id == run.bc_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    run = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if (
        connection is None
        or connection.kind != "OFFICIAL_MCP"
        or connection.status != "ACTIVE"
    ):
        raise DomainError("connection_unavailable", "当前租户 MCP 连接不可用")
    if (
        run.candidate_attempt_id is not None
        or run.mcp_candidate_attempt_id is not None
        or not run.bc_id
        or run.work.get("bc_id") != run.bc_id
        or run.authorization_revision != connection.authorization_revision
        or binding is None
        or binding.kind != "OFFICIAL_MCP"
        or binding.status == "DISABLED"
        or binding.authorization_revision != run.authorization_revision
        or binding.revision != run.binding_revision
        or run.status not in {"RUNNING", "ADMISSION_WAIT", "COMPLETE"}
    ):
        raise DomainError("discovery_stale", "BC 发现任务已失效")
    return run, connection


def mark_mcp_run_error(session: Session, *, run: DiscoveryRun, code: str) -> None:
    """调用者已经锁定并核验代数；失败只能影响当前 BC。"""
    binding = session.get(
        BCConnectionBinding, (run.tenant_id, run.bc_id, run.connection_id)
    )
    assert binding is not None
    binding.status = "ERROR"
    binding.last_error_code = code
    run.status = "ERROR"
    run.error_code = code
    run.claim_id = None
    run.claimed_until = None
    session.add_all([binding, run])


def verified_observation(
    session: Session, *, run: DiscoveryRun
) -> ConnectionToolObservation:
    try:
        observation_id = UUID(run.work["observation_id"])
    except KeyError, TypeError, ValueError:
        raise incomplete() from None
    observation = session.get(
        ConnectionToolObservation, observation_id, populate_existing=True
    )
    profile = load_mcp_protocol()
    if (
        observation is None
        or observation.tenant_id != run.tenant_id
        or observation.connection_id != run.connection_id
        or observation.candidate_attempt_id is not None
        or type(observation.call_evidence.get("authorization_revision")) is not int
        or observation.call_evidence.get("authorization_revision")
        != run.authorization_revision
        or not observation.pagination_complete
        or not _fresh(observation.observed_at)
        or observation.expected_contract_revision != profile.schema_manifest_sha256
        or observation.schema_digest
        != hashlib.sha256(
            json.dumps(
                observation.tool_schemas, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    ):
        raise incomplete()
    for contract in load_tool_contracts():
        if contract.operation.startswith("accounts."):
            verify_tool_schema(
                contract, observation.tool_schemas.get(contract.tool_name, {})
            )
    bc_id = run.work.get("bc_id")
    if (
        type(bc_id) is not str
        or not bc_id
        or len(bc_id) > 128
        or observation.call_evidence.get("business_centers_complete") is not True
        or type(observation.call_evidence.get("business_centers")) is not list
        or any(
            type(row) is not dict
            for row in observation.call_evidence.get("business_centers", [])
        )
        or not any(
            row.get("bc_id") == bc_id
            for row in observation.call_evidence.get("business_centers", [])
        )
    ):
        raise incomplete()
    return observation


def staged_pages(
    session: Session, *, run_id: UUID, stage: str, bc_id: str = ""
) -> list[DiscoveryStagedPage]:
    return list(
        session.exec(
            select(DiscoveryStagedPage)
            .where(
                DiscoveryStagedPage.run_id == run_id,
                DiscoveryStagedPage.stage == stage,
                DiscoveryStagedPage.bc_id == bc_id,
            )
            .order_by(col(DiscoveryStagedPage.page))
        ).all()
    )


def _verified_rows(
    pages: list[DiscoveryStagedPage],
    *,
    run: DiscoveryRun,
    schema_digest: str,
    stage: str,
    bc_id: str = "",
    max_pages: int = 2000,
) -> list[dict[str, Any]]:
    if not pages or [p.page for p in pages] != list(range(1, len(pages) + 1)):
        raise incomplete()
    totals = (pages[0].total_pages, pages[0].total_number)
    if len(pages) != totals[0] or totals[0] > max_pages:
        raise incomplete()
    rows = []
    for page in pages:
        if (
            (page.tenant_id, page.connection_id, page.run_id)
            != (run.tenant_id, run.connection_id, run.id)
            or page.bc_id != bc_id
            or page.schema_digest != schema_digest
            or not _fresh(page.observed_at)
            or (page.total_pages, page.total_number) != totals
            or page.last_page != (page.page == len(pages))
            or type(page.rows) is not list
            or len(page.rows) > PAGE_SIZE
            or any(type(row) is not dict for row in page.rows)
        ):
            raise incomplete()
        expected_kind = (
            "FULL_RESPONSE"
            if stage in {"SUBJECT", "AUTHORIZED"}
            else "EXPLICIT_IDS"
            if stage == "DETAILS"
            else "REMOTE"
        )
        if page.pagination_kind != expected_kind:
            raise incomplete()
        if stage != "DETAILS" and (
            page.page < len(pages) and len(page.rows) != PAGE_SIZE
        ):
            raise incomplete()
        rows.extend(page.rows)
    if stage != "DETAILS" and totals[1] is not None and totals[1] != len(rows):
        raise incomplete()
    return rows


def _indexed(rows: list[dict[str, Any]], *, key: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        identity = row.get(key)
        if (
            type(identity) is not str
            or not identity.strip()
            or len(identity) > 128
            or identity in result
        ):
            raise incomplete()
        result[identity] = row
    return result


def authorization_from_staging(subject: dict[str, Any]) -> AuthorizationFacts:
    try:
        return AuthorizationFacts(
            subject_id=subject["subject_id"],
            grant_id=None,
            issuer=subject["issuer"],
            resource=subject["resource"],
            scopes=tuple(subject["scopes"]),
            read_authorized=None,
            upload_authorized=subject.get("upload_authorized"),
            build_authorized=None,
            evidence_source="MCP_COMPLETE_DIRECTORY_READ",
            observed_at=datetime.fromisoformat(subject["observed_at"]),
        )
    except KeyError, TypeError, ValueError:
        raise incomplete() from None


def publish_mcp_directory(
    session: Session,
    *,
    context: TenantContext,
    run_id: UUID,
    claim_id: UUID | None = None,
    revision: int | None = None,
) -> None:
    run, connection = locked_mcp_run(session, context=context, run_id=run_id)
    if run.status == "COMPLETE":
        return
    if (claim_id is not None and run.claim_id != claim_id) or (
        revision is not None and run.revision != revision
    ):
        raise DomainError("discovery_stale", "发现任务的执行租约已失效")
    if run.work.get("stage") != "FINALIZE":
        raise incomplete()
    observation = verified_observation(session, run=run)
    all_pages = {
        stage: staged_pages(
            session,
            run_id=run.id,
            stage=stage,
            bc_id=run.work["bc_id"] if stage in {"ASSETS", "DETAILS", "ROLES"} else "",
        )
        for stage in STAGES
    }
    data = {
        stage: _verified_rows(
            pages,
            run=run,
            schema_digest=observation.schema_digest,
            stage=stage,
            bc_id=run.work["bc_id"] if stage in {"ASSETS", "DETAILS", "ROLES"} else "",
            max_pages=2000 if stage == "AUTHORIZED" else 1000,
        )
        for stage, pages in all_pages.items()
    }
    if len(data["SUBJECT"]) != 1:
        raise incomplete()
    facts = authorization_from_staging(data["SUBJECT"][0])
    profile = load_mcp_protocol()
    if (
        facts.issuer != profile.issuer
        or facts.resource != profile.resource
        or not facts.subject_id
        or not _fresh(facts.observed_at)
    ):
        raise incomplete()
    authorized = _indexed(data["AUTHORIZED"], key="advertiser_id")
    if any(
        p.call_evidence.get("completeness_source") != AUTHORIZED_LIST_SOURCE
        for p in all_pages["AUTHORIZED"]
    ):
        raise incomplete()
    bcs = _indexed(data["BCS"], key="bc_id")
    bc_id = run.work["bc_id"]
    if bc_id not in bcs:
        raise incomplete()
    assets = _indexed(data["ASSETS"], key="advertiser_id")
    details = _indexed(data["DETAILS"], key="advertiser_id")
    roles = _indexed(data["ROLES"], key="advertiser_id")
    if set(details) != set(assets) & set(authorized) or set(roles) != set(assets):
        raise incomplete()
    # 显式详情批次逐页对齐所选 BC 的授权交集，不能移用另一页的回执。
    if len(all_pages["DETAILS"]) != len(all_pages["ASSETS"]):
        raise incomplete()
    for asset_page, detail_page in zip(
        all_pages["ASSETS"], all_pages["DETAILS"], strict=True
    ):
        if {row["advertiser_id"] for row in detail_page.rows} != (
            {row["advertiser_id"] for row in asset_page.rows} & set(authorized)
        ):
            raise incomplete()
    if any(
        row.get("role") not in {None, "ADMIN", "OPERATOR", "ANALYST"}
        for row in roles.values()
    ):
        raise incomplete()
    for row in details.values():
        if any(
            type(row.get(key)) is not str
            for key in ("name", "currency", "timezone", "remote_status")
        ):
            raise incomplete()
    authorization = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.tenant_id == run.tenant_id,
            ConnectionAuthorization.connection_id == connection.id,
            ConnectionAuthorization.authorization_revision
            == run.authorization_revision,
        )
    ).one_or_none()
    if (
        authorization is None
        or authorization.upstream_subject != facts.subject_id
        or authorization.issuer != facts.issuer
        or authorization.resource != facts.resource
        or set(authorization.scopes) != set(facts.scopes)
    ):
        raise incomplete()
    binding = session.get(BCConnectionBinding, (run.tenant_id, bc_id, connection.id))
    assert binding is not None
    from .directory_merge import merge_directory_bc

    merge_directory_bc(
        session, run=run, bc_id=bc_id, bc_name=str(bcs[bc_id].get("name", ""))
    )
    params = {
        "tenant_id": run.tenant_id,
        "connection_id": run.connection_id,
        "run_id": run.id,
        "bc_id": bc_id,
    }
    SASession.execute(
        session,
        text("""
        UPDATE bc_account_access SET in_bc=false,authorized=false,active=false,can_upload=false,can_build=false
        WHERE tenant_id=:tenant_id AND connection_id=:connection_id AND bc_id=:bc_id AND last_seen_run_id IS DISTINCT FROM :run_id
    """),
        params,
    )
    binding.status = "ACTIVE"
    binding.last_error_code = None
    session.add(binding)
    session.exec(
        insert(BCDefaultRoute)
        .values(tenant_id=run.tenant_id, bc_id=bc_id, connection_id=connection.id)
        .on_conflict_do_nothing(index_elements=["tenant_id", "bc_id"])
    )
    # 完整目录核验后保存本次观察；账户上传角色由能力任务独立发布。
    authorization.permission_summary = {
        **authorization.permission_summary,
        "read_authorized": True if "mcp:tt4b" in facts.scopes else None,
        "upload_authorized": facts.upload_authorized,
    }
    authorization.source = facts.evidence_source
    authorization.verified_at = datetime.now(UTC)
    session.add(authorization)
    run.status = "COMPLETE"
    run.completed_at = datetime.now(UTC)
    run.error_code = None
    run.claim_id = None
    run.claimed_until = None
    session.add(run)
    session.add(
        AuditEvent(
            tenant_id=run.tenant_id,
            actor_id=context.actor_id,
            action="tiktok.mcp.directory.publish",
            target_id=str(connection.id),
            details={
                "run_id": str(run.id),
                "bc_id": bc_id,
                "account_count": len(assets),
            },
        )
    )
    session.flush()
