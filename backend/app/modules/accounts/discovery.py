"""Transactional directory mutations; no network is performed under these locks."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.modules.tenants.permissions import require_tenant

from .models import (
    AdvertiserAccount,
    AuthorizationAttempt,
    BCAccountAccess,
    DiscoveryRun,
    DiscoverySeen,
    ExternalAssetOwner,
    TenantBC,
    TikTokConnection,
)


def claim_external_asset(
    session: Session, *, kind: str, external_id: str, tenant_id: UUID
) -> bool:
    session.exec(
        insert(ExternalAssetOwner)
        .values(kind=kind, external_id=external_id, owner_tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["kind", "external_id"])
    )
    owner = session.get(ExternalAssetOwner, (kind, external_id), populate_existing=True)
    assert owner is not None
    return owner.owner_tenant_id == tenant_id


def locked_run(session: Session, run_id: UUID) -> DiscoveryRun:
    run = session.exec(
        select(DiscoveryRun)
        .where(DiscoveryRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None:
        raise DomainError("discovery_not_found", "未找到发现任务")
    return run


def validate_run(session: Session, run: DiscoveryRun) -> TikTokConnection:
    require_tenant(
        session, actor_id=run.actor_id, tenant_id=run.tenant_id, action="manage"
    )
    connection = session.exec(
        select(TikTokConnection)
        .where(
            TikTokConnection.id == run.connection_id,
            TikTokConnection.tenant_id == run.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if connection is None or connection.status == "DISABLED":
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    if connection.credential_revision != run.credential_revision:
        raise DomainError("discovery_stale", "发现任务凭据版本已过期")
    if run.candidate_attempt_id:
        attempt = session.get(
            AuthorizationAttempt, run.candidate_attempt_id, populate_existing=True
        )
        if (
            attempt is None
            or attempt.tenant_id != run.tenant_id
            or attempt.connection_id != run.connection_id
            or attempt.actor_id != run.actor_id
            or attempt.status != "CANDIDATE_READY"
            or attempt.base_credential_revision != run.credential_revision
            or not attempt.candidate_ciphertext
        ):
            raise DomainError("discovery_stale", "候选授权已过期")
    elif connection.status != "ACTIVE":
        raise DomainError("connection_unavailable", "当前租户连接不可用")
    return connection


def save_directory_page(
    session: Session,
    *,
    run_id: UUID,
    bc_id: str,
    page: int,
    rows: list[dict],
    authorized_ids: set[str],
    last_page: bool,
) -> None:
    run = locked_run(session, run_id)
    if run.status not in {"RUNNING", "ADMISSION_WAIT"}:
        raise DomainError("discovery_stale", "发现任务已结束")
    validate_run(session, run)
    if session.get(DiscoverySeen, (run.id, bc_id, page)):
        return
    if page < 1 or (
        page > 1 and not session.get(DiscoverySeen, (run.id, bc_id, page - 1))
    ):
        raise DomainError("discovery_incomplete", "账户分页不连续")
    prior = session.exec(
        select(DiscoverySeen).where(
            DiscoverySeen.run_id == run.id,
            DiscoverySeen.bc_id == bc_id,
            DiscoverySeen.last_page,
        )
    ).first()  # noqa: E712
    if prior:
        raise DomainError("discovery_incomplete", "账户末页之后不能继续写入")
    bc = session.get(TenantBC, (run.tenant_id, bc_id))
    if not bc:
        raise DomainError("discovery_incomplete", "BC 尚未发现")
    bc.ownership_conflict = not claim_external_asset(
        session, kind="BC", external_id=bc_id, tenant_id=run.tenant_id
    )
    for row in rows:
        advertiser_id = row.get("advertiser_id")
        if not isinstance(advertiser_id, str) or not advertiser_id:
            raise DomainError("unsupported_account_schema", "账户 ID 必须为字符串")
        own = claim_external_asset(
            session,
            kind="ADVERTISER",
            external_id=advertiser_id,
            tenant_id=run.tenant_id,
        )
        values: dict[str, Any] = {
            key: str(row.get(key) or "") for key in ("name", "currency", "timezone")
        }
        values.update(
            remote_status=str(row.get("remote_status") or "UNKNOWN"),
            ownership_conflict=not own,
        )
        session.exec(
            insert(AdvertiserAccount)
            .values(tenant_id=run.tenant_id, advertiser_id=advertiser_id, **values)
            .on_conflict_do_update(
                index_elements=["tenant_id", "advertiser_id"], set_=values
            )
        )
        metadata_ok = bool(values["currency"] and values["timezone"])
        # No SDK capability map is approved. Even known enabled status cannot grant actions.
        usable = (
            own
            and not bc.ownership_conflict
            and metadata_ok
            and advertiser_id in authorized_ids
        )
        access_values = {
            "in_bc": True,
            "authorized": advertiser_id in authorized_ids,
            "active": usable and values["remote_status"] in {"STATUS_ENABLE", "ENABLE"},
            "can_upload": False,
            "can_build": False,
            "permission_state": "UNKNOWN" if metadata_ok else "METADATA_INCOMPLETE",
            "last_seen_run_id": run.id,
            "checked_at": datetime.now(UTC),
        }
        session.exec(
            insert(BCAccountAccess)
            .values(
                tenant_id=run.tenant_id,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                connection_id=run.connection_id,
                **access_values,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "bc_id", "advertiser_id", "connection_id"],
                set_=access_values,
            )
        )
    session.add(
        DiscoverySeen(
            run_id=run.id,
            bc_id=bc_id,
            page=page,
            last_page=last_page,
            processed_count=len(rows),
        )
    )
    session.flush()


def _complete(pages: list[DiscoverySeen]) -> bool:
    return (
        bool(pages)
        and [p.page for p in pages] == list(range(1, len(pages) + 1))
        and all(not p.last_page for p in pages[:-1])
        and pages[-1].last_page
    )


def finalize_directory(session: Session, *, run_id: UUID) -> None:
    run = locked_run(session, run_id)
    if run.status == "COMPLETE":
        return
    if run.status not in {"RUNNING", "ADMISSION_WAIT"}:
        raise DomainError("discovery_stale", "发现任务已结束")
    connection = validate_run(session, run)
    pages = session.exec(
        select(DiscoverySeen)
        .where(DiscoverySeen.run_id == run.id)
        .order_by(col(DiscoverySeen.bc_id), col(DiscoverySeen.page))
    ).all()
    bc_ids = run.work.get("bc_ids", [])
    if (
        run.work.get("stage") != "FINALIZE"
        or not _complete([p for p in pages if p.bc_id == ""])
        or any(
            not _complete([p for p in pages if p.bc_id == bc_id]) for bc_id in bc_ids
        )
    ):
        raise DomainError("discovery_incomplete", "尚未完整扫描所有 BC 和账户分页")
    accesses = session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == run.tenant_id,
            BCAccountAccess.connection_id == run.connection_id,
        )
    ).all()
    for access in accesses:
        if access.last_seen_run_id != run.id:
            access.active = False
            access.in_bc = False
    if run.candidate_attempt_id:
        attempt = session.get(AuthorizationAttempt, run.candidate_attempt_id)
        assert attempt is not None
        connection.credential_ciphertext = attempt.candidate_ciphertext
        connection.credential_revision += 1
        connection.status = "ACTIVE"
        attempt.status = "ACCEPTED"
        attempt.candidate_ciphertext = None
    run.status = "COMPLETE"
    run.completed_at = datetime.now(UTC)
    run.error_code = None
    run.claim_id = None
    run.claimed_until = None
    session.flush()
