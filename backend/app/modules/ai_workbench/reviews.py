"""Durable DSH review tasks and frozen evidence assembly."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime, ForeignKeyConstraint, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import (
    sanitize_public,
    snapshot_knowledge_refs,
)
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerVersion,
    BuildOccurrence,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.automation_rules.models import AutomationRuleEvidence


def _now() -> datetime:
    return datetime.now(UTC)


class DSHReviewTask(SQLModel, table=True):
    """One fenced, tenant-scoped DSH review request."""

    __tablename__ = "ai_dsh_review_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_ai_dsh_review_request"),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_dsh_review_optimizer",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    optimizer_id: UUID
    request_id: UUID
    request_digest: str = Field(default="", max_length=64)
    dsh_task_id: str = Field(max_length=160)
    status: str = Field(default="PENDING", max_length=16)
    authorization_expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    fence: str = Field(max_length=64)
    restart_generation: int = 0
    cancellation_requested: bool = False
    optimizer_version: int
    evidence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=DateTime(timezone=True))
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


def authorize(session: Session, context: TenantContext, *, write: bool = False) -> None:
    # Import lazily: permissions resolves ``app.models.User`` and importing it
    # while the metadata registry is loading would create a circular import
    # for direct DSH worker consumers.
    from app.modules.tenants.permissions import require_tenant

    require_tenant(
        session,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        action="strategy_write" if write else "read",
    )


def load_optimizer(session: Session, context: TenantContext, optimizer_id: UUID) -> tuple[AIOptimizer, AIOptimizerVersion]:
    optimizer = session.exec(select(AIOptimizer).where(
        AIOptimizer.tenant_id == context.tenant_id, AIOptimizer.id == optimizer_id,
    )).one_or_none()
    if optimizer is None:
        raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
    version = session.exec(select(AIOptimizerVersion).where(
        AIOptimizerVersion.tenant_id == context.tenant_id,
        AIOptimizerVersion.optimizer_id == optimizer.id,
        AIOptimizerVersion.number == optimizer.latest_version,
    )).one_or_none()
    if version is None:
        raise DomainError("ai_optimizer_version_missing", "ai_optimizer_version_missing")
    return optimizer, version


def _digest(value: Any) -> str:
    payload = json.dumps(
        sanitize_public(value), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _reporting_frozen_refs(
    session: Session,
    *,
    context: TenantContext,
    version: AIOptimizerVersion,
) -> list[dict[str, Any]]:
    """Return immutable query/evidence references, never live report rows."""
    from app.modules.reporting.query_models import (
        QuerySnapshot,
        QuerySnapshotEvidence,
        QuerySnapshotNameVersion,
        QuerySnapshotRow,
    )
    from app.modules.reporting.read_models.evidence import ReportEvidenceVersion

    snapshots = session.exec(
        select(QuerySnapshot).where(
            QuerySnapshot.tenant_id == context.tenant_id,
            QuerySnapshot.bc_id == version.bc_id,
            QuerySnapshot.actor_id == context.actor_id,
        ).order_by(col(QuerySnapshot.created_at).desc())
    ).all()
    account_ids = set(version.advertiser_ids)
    snapshots = [
        row for row in snapshots
        if account_ids.intersection(set(row.advertiser_ids or ()))
    ]
    if not snapshots:
        return []

    snapshot_ids = [row.id for row in snapshots]
    evidence_rows = session.exec(
        select(QuerySnapshotEvidence).where(
            col(QuerySnapshotEvidence.snapshot_id).in_(snapshot_ids)
        )
    ).all()
    name_rows = session.exec(
        select(QuerySnapshotNameVersion).where(
            col(QuerySnapshotNameVersion.snapshot_id).in_(snapshot_ids)
        )
    ).all()
    snapshot_rows = session.exec(
        select(QuerySnapshotRow).where(
            col(QuerySnapshotRow.snapshot_id).in_(snapshot_ids)
        )
    ).all()
    version_ids = {row.version_id for row in evidence_rows}
    version_ids.update(row.version_id for row in name_rows)
    # Newer reporting migrations also keep the frozen IDs inline on each row.
    # Read those IDs as a fallback so the review contract remains immutable
    # across both storage layouts during a rolling deployment.
    for row in snapshot_rows:
        version_ids.update(row.evidence_versions or ())
    evidence_versions = session.exec(
        select(ReportEvidenceVersion).where(
            col(ReportEvidenceVersion.id).in_(version_ids),
            ReportEvidenceVersion.tenant_id == context.tenant_id,
            col(ReportEvidenceVersion.advertiser_id).in_(version.advertiser_ids),
        )
    ).all() if version_ids else []
    evidence_by_id = {row.id: row for row in evidence_versions}
    if len(evidence_by_id) != len(version_ids):
        raise DomainError(
            "ai_evidence_not_found",
            "reporting snapshot evidence is incomplete or outside optimizer scope",
        )
    if any(
        row.tenant_id != context.tenant_id
        or row.advertiser_id not in set(version.advertiser_ids)
        for row in evidence_versions
    ):
        raise DomainError(
            "ai_evidence_not_found",
            "reporting snapshot evidence is outside optimizer scope",
        )
    refs_by_snapshot: dict[UUID, list[dict[str, Any]]] = {}
    seen_refs: set[tuple[UUID, str | None, UUID]] = set()

    def add_ref(snapshot_id: UUID, version_id: UUID, *, category: str, row_key: str | None = None) -> None:
        dedupe_key = (snapshot_id, row_key, version_id)
        if dedupe_key in seen_refs:
            return
        seen_refs.add(dedupe_key)
        evidence = evidence_by_id.get(version_id)
        ref: dict[str, Any] = {"version_id": str(version_id), "category": category}
        if row_key is not None:
            ref["row_key"] = row_key
        if evidence is not None:
            ref.update({
                "advertiser_id": evidence.advertiser_id,
                "kind": evidence.kind,
                "refs": evidence.refs,
                "material_uses": evidence.material_uses,
                "naming_versions": evidence.naming_versions,
                "created_at": evidence.created_at,
                "retired_at": evidence.retired_at,
            })
        ref["digest"] = _digest(ref)
        refs_by_snapshot.setdefault(snapshot_id, []).append(ref)

    for row in evidence_rows:
        add_ref(row.snapshot_id, row.version_id, category="row", row_key=row.row_key)
    for row in name_rows:
        add_ref(row.snapshot_id, row.version_id, category="name")
    for row in snapshot_rows:
        for version_id in row.evidence_versions or ():
            add_ref(row.snapshot_id, version_id, category="inline", row_key=row.row_key)

    result: list[dict[str, Any]] = []
    for row in snapshots:
        snapshot = {
            "id": str(row.id),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "bc_id": row.bc_id,
            "advertiser_ids": row.advertiser_ids,
            "filter_digest": row.filter_digest,
            "dependency_digest": row.dependency_digest,
            "filters": row.filters,
            "naming_versions": row.naming_versions,
            "dependency_stamp": row.dependency_stamp,
            "total": row.total,
            "summary": row.summary,
            "coverage": row.coverage,
            "publication_versions": row.publication_versions,
            "evidence": refs_by_snapshot.get(row.id, []),
        }
        snapshot["digest"] = _digest(snapshot)
        result.append(snapshot)
    return result


def frozen_evidence_ids(evidence: Mapping[str, Any] | None) -> set[str]:
    """Collect identifiers for every frozen evidence category.

    Review proposals may cite report snapshots, business runs, operation
    exports, build occurrences, plans, or rule evidence.  Keeping this list
    explicit prevents a proposal from citing a live row that was never frozen.
    """
    if not isinstance(evidence, Mapping):
        return set()

    fields_by_category: dict[str, tuple[str, ...]] = {
        "plans": ("id",),
        "build_occurrences": ("id", "strategy_version_id"),
        "rule_evidence": ("id",),
        "reporting": ("id", "version_id"),
        "business": ("id", "run_id"),
        "operation_logs": ("id", "export_id"),
    }
    result: set[str] = set()
    for category, fields in fields_by_category.items():
        value = evidence.get(category)
        if category == "reporting" and isinstance(value, Mapping):
            rows = value.get("snapshots", ())
            nested = []
            for snapshot in rows if isinstance(rows, list) else ():
                if isinstance(snapshot, Mapping):
                    nested.append(snapshot)
                    refs = snapshot.get("evidence", ())
                    if isinstance(refs, list):
                        nested.extend(refs)
            rows = nested
        elif category == "business" and isinstance(value, Mapping):
            rows = []
            for key in ("runs", "publications"):
                candidate = value.get(key, ())
                if isinstance(candidate, list):
                    rows.extend(candidate)
        elif category == "operation_logs" and isinstance(value, Mapping):
            rows = []
            for key in ("coverage", "exports"):
                candidate = value.get(key, ())
                if isinstance(candidate, list):
                    rows.extend(candidate)
        else:
            rows = value if isinstance(value, list) else ()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for field in fields:
                item = row.get(field)
                if item is not None:
                    result.add(str(item))
    return result


def frozen_evidence(
    session: Session,
    *,
    context: TenantContext,
    optimizer: AIOptimizer,
    version: AIOptimizerVersion,
    evidence_ids: tuple[UUID, ...] = (),
    knowledge_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble read-only snapshots from existing plans, builds and rule evidence."""
    plan_rows = session.exec(select(AIDailyPlanRow).where(
        AIDailyPlanRow.tenant_id == context.tenant_id,
        AIDailyPlanRow.optimizer_id == optimizer.id,
    ).order_by(col(AIDailyPlanRow.business_date), col(AIDailyPlanRow.position))).all()
    occurrences = session.exec(select(BuildOccurrence).where(
        BuildOccurrence.tenant_id == context.tenant_id,
        BuildOccurrence.optimizer_id == optimizer.id,
    ).order_by(col(BuildOccurrence.scheduled_at))).all()
    all_rule_evidence = session.exec(
        select(AutomationRuleEvidence).where(
            AutomationRuleEvidence.tenant_id == context.tenant_id,
            AutomationRuleEvidence.optimizer_id == optimizer.id,
        ).order_by(col(AutomationRuleEvidence.created_at))
    ).all()
    rule_evidence = (
        [row for row in all_rule_evidence if row.id in set(evidence_ids)]
        if evidence_ids
        else all_rule_evidence
    )
    # Freeze IDs, revisions and digests from append-only reporting, business and
    # operation stores so later source updates never change this review input.
    from app.modules.business.models import BusinessPublication, BusinessRun
    from app.modules.operation_logs.models import OperationCoverage, OperationExport
    reporting_snapshots = _reporting_frozen_refs(
        session, context=context, version=version
    )
    business_scopes = session.exec(select(BusinessRun).where(BusinessRun.tenant_id == context.tenant_id, BusinessRun.bc_id == version.bc_id).order_by(col(BusinessRun.created_at).desc()).limit(50)).all()
    business_pubs = session.exec(select(BusinessPublication).where(BusinessPublication.tenant_id == context.tenant_id, BusinessPublication.bc_id == version.bc_id)).all()
    operation_coverage = session.exec(
        select(OperationCoverage)
        .join(
            OperationExport,
            (OperationExport.id == OperationCoverage.export_id)
            & (OperationExport.tenant_id == OperationCoverage.tenant_id)
            & (OperationExport.advertiser_id == OperationCoverage.advertiser_id),
        )
        .where(
            OperationCoverage.tenant_id == context.tenant_id,
            col(OperationCoverage.advertiser_id).in_(version.advertiser_ids),
            OperationExport.bc_id == version.bc_id,
        )
    ).all()
    operation_exports = session.exec(
        select(OperationExport)
        .where(
            OperationExport.tenant_id == context.tenant_id,
            OperationExport.bc_id == version.bc_id,
            col(OperationExport.advertiser_id).in_(version.advertiser_ids),
        )
        .order_by(col(OperationExport.observed_at).desc())
        .limit(50)
    ).all()
    # These snapshots intentionally retain IDs/status/version only.  Full report
    # and business rows remain in their own append-only stores and are referenced.
    frozen = sanitize_public({
        "optimizer": {
            "id": str(optimizer.id),
            "version": version.number,
            "bc_id": version.bc_id,
            "advertiser_ids": list(version.advertiser_ids),
            "optimization_mode": version.optimization_mode,
            "authorization_limits": version.authorization_limits,
            "strategy_binding_id": str(version.strategy_binding_id),
        },
        "plans": [{
            "id": str(row.id), "business_date": str(row.business_date),
            "title": row.title, "position": row.position,
        } for row in plan_rows],
        "build_occurrences": [{
            "id": str(row.id), "status": row.status, "scheduled_at": row.scheduled_at,
            "strategy_version_id": str(row.strategy_version_id), "evidence": row.evidence,
        } for row in occurrences],
        "rule_evidence": [{
            "id": str(row.id), "rule_id": str(row.rule_id) if row.rule_id else None,
            "rule_version": row.rule_version, "created_at": row.created_at,
            "data_freshness": row.data_freshness, "publication_versions": row.publication_versions,
            "checked_objects": row.checked_objects, "hits": row.hits,
            "action_ledger_refs": row.action_ledger_refs, "management_tasks": row.management_tasks,
            "readback_status": row.readback_status,
        } for row in rule_evidence],
        # Reporting/business/operation stores are append-only sources owned by
        # their modules.  Freeze their identity/version references here instead
        # of re-querying live rows during a later DSH decision.
        "reporting": {
            "source": "reporting_store",
            "account_ids": list(version.advertiser_ids),
            "optimizer_version": version.number,
            "snapshots": reporting_snapshots,
        },
        "business": {
            "source": "business_store",
            "bc_id": version.bc_id,
            "optimizer_version": version.number,
            "runs": [{"id": str(row.id), "scope_revision": row.scope_revision, "source_signature": row.source_signature, "status": row.status, "finished_at": row.finished_at} for row in business_scopes],
            "publications": [{"quarter": row.quarter, "run_id": str(row.run_id)} for row in business_pubs],
        },
        "operation_logs": {
            "source": "operation_log_store",
            "account_ids": list(version.advertiser_ids),
            "optimizer_version": version.number,
            "coverage": [{"advertiser_id": row.advertiser_id, "stat_date": row.stat_date, "export_id": str(row.export_id), "digest": row.digest, "revision": row.revision} for row in operation_coverage],
            "exports": [{"id": str(row.id), "digest": row.digest, "parser_version": row.parser_version, "observed_at": row.observed_at} for row in operation_exports],
        },
        "knowledge_refs": snapshot_knowledge_refs(knowledge_refs),
        "captured_at": _now(),
    })
    requested = {str(item) for item in evidence_ids}
    if requested and not requested.issubset(frozen_evidence_ids(frozen)):
        raise DomainError("ai_evidence_not_found", "requested evidence is outside optimizer scope")
    return frozen


def create_review_task(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    request_id: UUID,
    evidence_ids: tuple[UUID, ...] = (),
    knowledge_refs: list[dict[str, Any]] | None = None,
    authorization_ttl_seconds: int = 300,
    dsh_task_id: str | None = None,
    request_digest: str | None = None,
) -> DSHReviewTask:
    authorize(session, context, write=True)
    if not 30 <= authorization_ttl_seconds <= 900:
        raise DomainError("ai_dsh_authorization_invalid", "authorization TTL must be 30..900 seconds")
    optimizer, version = load_optimizer(session, context, optimizer_id)
    existing = session.exec(select(DSHReviewTask).where(
        DSHReviewTask.tenant_id == context.tenant_id, DSHReviewTask.request_id == request_id,
    )).one_or_none()
    if existing is not None:
        if existing.optimizer_id != optimizer.id:
            raise DomainError("ai_review_scope_mismatch", "request is already used by another optimizer")
        if request_digest is not None and existing.request_digest != request_digest:
            raise DomainError("idempotency_conflict", "review request payload differs")
        return existing
    request_digest = request_digest or hashlib.sha256(
        json.dumps({"evidence_ids": [str(item) for item in evidence_ids], "knowledge_refs": knowledge_refs or [], "authorization_ttl_seconds": authorization_ttl_seconds, "dsh_task_id": dsh_task_id}, sort_keys=True, default=str).encode()
    ).hexdigest()
    task = DSHReviewTask(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        request_id=request_id,
        request_digest=request_digest,
        dsh_task_id=dsh_task_id or f"dsh:{context.tenant_id}:{request_id}",
        authorization_expires_at=_now() + timedelta(seconds=authorization_ttl_seconds),
        fence=uuid4().hex,
        optimizer_version=version.number,
        evidence=frozen_evidence(session, context=context, optimizer=optimizer, version=version,
                                 evidence_ids=evidence_ids, knowledge_refs=knowledge_refs),
        created_by=context.actor_id,
    )
    session.add(task)
    session.flush()
    return task


def get_review_task(session: Session, *, context: TenantContext, optimizer_id: UUID, task_id: UUID) -> DSHReviewTask:
    authorize(session, context)
    row = session.exec(select(DSHReviewTask).where(
        DSHReviewTask.tenant_id == context.tenant_id,
        DSHReviewTask.optimizer_id == optimizer_id,
        DSHReviewTask.id == task_id,
    )).one_or_none()
    if row is None:
        raise DomainError("ai_review_not_found", "ai_review_not_found")
    return row
