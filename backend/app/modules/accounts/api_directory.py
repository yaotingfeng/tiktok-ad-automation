"""官方 API 候选多 BC 目录：完整暂存后一次发布，不按页替换 live grants。"""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.discovery import AUTHORIZED_LIST_SOURCE
from app.integrations.tiktok.official.authorization import (
    CONTRACT_REVISION,
    material_authorization,
)
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.directory_merge import merge_directory_bc
from app.modules.accounts.discovery import validate_run
from app.modules.accounts.mcp_discovery import (
    _fresh,
    _indexed,
    _verified_rows,
    incomplete,
    staged_pages,
)
from app.modules.accounts.models import (
    AuthorizationAttempt,
    DiscoveryRun,
    TikTokConnection,
)

SCHEMA_DIGEST = sha256(CONTRACT_REVISION.encode()).hexdigest()


def publish_api_directory(
    session: Session, *, context: TenantContext, run_id: UUID
) -> None:
    identity = session.get(DiscoveryRun, run_id)
    if identity is None or (identity.tenant_id, identity.actor_id) != (
        context.tenant_id,
        context.actor_id,
    ):
        raise DomainError("discovery_not_found", "当前租户发现任务不存在")
    session.exec(
        select(TikTokConnection)
        .where(TikTokConnection.id == identity.connection_id)
        .with_for_update()
    ).one()
    run = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if run.status == "COMPLETE":
        return
    connection = validate_run(session, run)
    if connection.kind != "OFFICIAL_API" or run.work.get("stage") != "FINALIZE":
        raise incomplete()

    def rows(stage: str, bc_id: str = "") -> list[dict[str, Any]]:
        pages = staged_pages(session, run_id=run.id, stage=stage, bc_id=bc_id)
        return _verified_rows(
            pages, run=run, schema_digest=SCHEMA_DIGEST, stage=stage, bc_id=bc_id
        )

    subjects = rows("SUBJECT")
    if len(subjects) != 1:
        raise incomplete()
    attempt = (
        session.get(AuthorizationAttempt, run.candidate_attempt_id)
        if run.candidate_attempt_id
        else None
    )
    private = decrypt_credentials(
        tenant_id=run.tenant_id,
        ciphertext=(
            attempt.candidate_ciphertext
            if attempt
            else connection.credential_ciphertext
        )
        or "",
    )
    try:
        facts = material_authorization(
            private, observed_at=datetime.fromisoformat(subjects[0]["observed_at"])
        )
    except KeyError, ValueError, TypeError:
        raise incomplete() from None
    if not _fresh(facts.observed_at):
        raise incomplete()
    if (
        subjects[0]["scopes"] != list(facts.scopes)
        or subjects[0]["issuer"] != facts.issuer
        or subjects[0]["resource"] != facts.resource
    ):
        raise incomplete()
    authorized = _indexed(rows("AUTHORIZED"), key="advertiser_id")
    if any(
        p.call_evidence.get("completeness_source") != AUTHORIZED_LIST_SOURCE
        for p in staged_pages(session, run_id=run.id, stage="AUTHORIZED")
    ):
        raise incomplete()
    bcs = _indexed(rows("BCS"), key="bc_id")
    # 先验证所有 BC 的完整页及交集，再进行任何 live 目录修改。
    observed_details: dict[str, dict[str, Any]] = {}
    for bc_id in bcs:
        assets = _indexed(rows("ASSETS", bc_id), key="advertiser_id")
        details = _indexed(rows("DETAILS", bc_id), key="advertiser_id")
        roles = _indexed(rows("ROLES", bc_id), key="advertiser_id")
        for advertiser_id, detail in details.items():
            if (
                advertiser_id in observed_details
                and observed_details[advertiser_id] != detail
            ):
                raise incomplete()
            observed_details[advertiser_id] = detail
        if set(details) != set(assets) & set(authorized) or set(roles) != set(assets):
            raise incomplete()
        if any(
            row.get("role") not in {None, "ADMIN", "OPERATOR", "ANALYST"}
            for row in roles.values()
        ):
            raise incomplete()
        assets_pages = staged_pages(session, run_id=run.id, stage="ASSETS", bc_id=bc_id)
        details_pages = staged_pages(
            session, run_id=run.id, stage="DETAILS", bc_id=bc_id
        )
        if len(assets_pages) != len(details_pages):
            raise incomplete()
        for assets_page, details_page in zip(assets_pages, details_pages, strict=True):
            if {r["advertiser_id"] for r in details_page.rows} != (
                {r["advertiser_id"] for r in assets_page.rows} & set(authorized)
            ):
                raise incomplete()
    for bc_id, bc in bcs.items():
        merge_directory_bc(
            session, run=run, bc_id=bc_id, bc_name=str(bc.get("name", ""))
        )
        session.exec(
            insert(BCConnectionBinding)
            .values(
                tenant_id=run.tenant_id,
                bc_id=bc_id,
                connection_id=connection.id,
                kind=connection.kind,
            )
            .on_conflict_do_nothing()
        )
    SASession.execute(
        session,
        text("""UPDATE bc_account_access SET in_bc=false,authorized=false,active=false,can_upload=false,can_build=false
        WHERE tenant_id=:tenant_id AND connection_id=:connection_id AND last_seen_run_id IS DISTINCT FROM :run_id"""),
        {"tenant_id": run.tenant_id, "connection_id": connection.id, "run_id": run.id},
    )
    previous = session.exec(
        select(ConnectionAuthorization).where(
            ConnectionAuthorization.connection_id == connection.id,
            ConnectionAuthorization.authorization_revision
            == connection.authorization_revision,
        )
    ).one_or_none()
    if attempt:
        connection.credential_revision += 1
        connection.authorization_revision += 1
        connection.credential_ciphertext = attempt.candidate_ciphertext
        attempt.candidate_ciphertext = None
        attempt.status = "ACCEPTED"
        session.add(attempt)
    connection.status = "ACTIVE"
    connection.adapter_contract_revision = CONTRACT_REVISION
    summary = {
        "read_authorized": True if facts.build_authorized is not None else None,
        "build_authorized": facts.build_authorized,
        "upload_authorized": facts.upload_authorized,
    }
    if previous is not None and not attempt:
        previous.permission_summary = summary
        previous.verified_at = datetime.now(UTC)
        session.add(previous)
    else:
        session.add(
            ConnectionAuthorization(
                tenant_id=run.tenant_id,
                connection_id=connection.id,
                authorization_revision=connection.authorization_revision,
                upstream_subject=None,
                upstream_grant_id=None,
                issuer=facts.issuer,
                resource=facts.resource,
                scopes=list(facts.scopes),
                permission_summary=summary,
                source="OFFICIAL_TOKEN_SCOPE_AND_COMPLETE_DIRECTORY",
                verified_at=datetime.now(UTC),
                previous_authorization_id=previous.id if previous else None,
            )
        )
    run.status, run.error_code, run.completed_at = "COMPLETE", None, datetime.now(UTC)
    run.claim_id = run.claimed_until = None
    session.add_all([run, connection])
    session.flush()
