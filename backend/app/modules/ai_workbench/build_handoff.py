"""Scheduled AI optimizer handoff into the existing TK-ADA build pipeline.

This module owns only the local occurrence ledger.  It never calls TikTok or a
provider directly; preparation, preview and submission remain delegated to the
existing builds services and their durable outbox tasks.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.ai_workbench.models import (
    AIOptimizer,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
    BuildOccurrence,
)
from app.modules.ai_workbench.plans import AIDailyPlanRow
from app.modules.builds import drafts, previews, submissions
from app.modules.builds.preview_models import BuildPreview
from app.modules.providers.models import ProviderApplication
from app.modules.tenants.permissions import require_tenant

OCCURRENCE_NAMESPACE = UUID("63c5c8b5-4c82-4d9d-938b-6f3091daec85")
TASK_NAME = "ai_workbench.dispatch_occurrence"
register_dispatch_task(TASK_NAME, "builds")
TERMINAL = frozenset({"COMPLETE", "FAILED", "UNKNOWN", "PARTIAL"})
MATERIAL_WAIT_ERRORS = frozenset(
    {
        "material_unavailable",
        "material_not_ready",
        "material_preparing",
        "draft_inputs_empty",
        "preview_not_ready",
        "preview_incomplete",
        "copy_material_unavailable",
    }
)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _scheduled_at(row: AIDailyPlanRow) -> datetime:
    # The plan contract has no optimizer timezone yet.  Store a timezone-aware
    # value at the boundary so ordering/uniqueness are deterministic in UTC.
    return datetime.combine(row.business_date, row.scheduled_time).replace(tzinfo=UTC)


class BuildHandoff:
    """Resolve and execute one tenant-scoped daily-plan row occurrence."""

    def __init__(self, session: Session, *, context: TenantContext):
        self.session = session
        self.context = context

    def _authorize(self) -> None:
        require_tenant(
            self.session,
            actor_id=self.context.actor_id,
            tenant_id=self.context.tenant_id,
            action="build",
        )

    def _resolve_snapshot(
        self,
        plan_row_id: UUID,
        *,
        caller_strategy_version_id: UUID | None,
    ) -> tuple[AIDailyPlanRow, AIOptimizer, AIOptimizerVersion, UUID]:
        row = self.session.exec(
            select(AIDailyPlanRow)
            .where(
                AIDailyPlanRow.tenant_id == self.context.tenant_id,
                AIDailyPlanRow.id == plan_row_id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise DomainError("ai_daily_plan_row_not_found", "ai_daily_plan_row_not_found")
        optimizer = self.session.exec(
            select(AIOptimizer)
            .where(
                AIOptimizer.tenant_id == self.context.tenant_id,
                AIOptimizer.id == row.optimizer_id,
            )
            .with_for_update()
        ).one_or_none()
        if optimizer is None:
            raise DomainError("ai_optimizer_not_found", "ai_optimizer_not_found")
        if not optimizer.active:
            raise DomainError("ai_optimizer_inactive", "ai_optimizer_inactive")
        binding = self.session.exec(
            select(AIOptimizerStrategyBinding).where(
                AIOptimizerStrategyBinding.tenant_id == self.context.tenant_id,
                AIOptimizerStrategyBinding.optimizer_id == optimizer.id,
                AIOptimizerStrategyBinding.active.is_(True),
            )
        ).one_or_none()
        if binding is None:
            raise DomainError("ai_strategy_binding_required", "ai_strategy_binding_required")
        if caller_strategy_version_id is not None and caller_strategy_version_id != binding.strategy_version_id:
            raise DomainError("ai_strategy_binding_mismatch", "ai_strategy_binding_mismatch")
        version = self.session.exec(
            select(AIOptimizerVersion).where(
                AIOptimizerVersion.tenant_id == self.context.tenant_id,
                AIOptimizerVersion.optimizer_id == optimizer.id,
                AIOptimizerVersion.number == optimizer.latest_version,
            )
        ).one_or_none()
        if version is None:
            raise DomainError("ai_optimizer_version_missing", "ai_optimizer_version_missing")
        if version.strategy_binding_id != binding.id:
            raise DomainError("ai_strategy_binding_mismatch", "ai_strategy_binding_mismatch")
        # Provider rows are validated when the plan is saved; repeat the scoped
        # check here because an application can be disabled after scheduling.
        application = self.session.exec(
            select(ProviderApplication).where(
                ProviderApplication.tenant_id == self.context.tenant_id,
                ProviderApplication.connection_id == row.provider_connection_id,
                ProviderApplication.external_id == row.application_id,
            )
        ).one_or_none()
        if application is None:
            raise DomainError("ai_provider_application_not_found", "ai_provider_application_not_found")
        return row, optimizer, version, binding.strategy_version_id

    def create_occurrence(
        self,
        plan_row_id: UUID,
        request_id: UUID,
        *,
        strategy_version_id: UUID | None = None,
    ) -> BuildOccurrence:
        """Create or return a frozen occurrence and its local outbox message.

        A caller may provide a strategy only as an assertion.  It can never
        select a cross-optimizer or historical strategy version.
        """
        self._authorize()
        row, optimizer, version, resolved_strategy = self._resolve_snapshot(
            plan_row_id, caller_strategy_version_id=strategy_version_id
        )
        scheduled_at = _scheduled_at(row)
        payload = {
            "optimizer_id": str(optimizer.id),
            "plan_row_id": str(row.id),
            "strategy_version_id": str(resolved_strategy),
            "scheduled_at": scheduled_at.isoformat(),
            "bc_id": version.bc_id,
            "advertiser_ids": list(version.advertiser_ids),
            "provider_connection_id": str(row.provider_connection_id),
            "application_id": row.application_id,
            "title": row.title,
            "promotion_url": row.promotion_url,
            "external_drama_id": row.external_drama_id,
            "order_materials": row.order_materials,
        }
        digest = _digest(payload)
        # Serialize request and schedule checks without relying on caller-side
        # Celery task IDs for deduplication.
        key = int.from_bytes(hashlib.sha256(f"ai-occurrence:{self.context.tenant_id}:{request_id}".encode()).digest()[:8], signed=True)
        SQLAlchemySession.execute(self.session, text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        prior = self.session.exec(
            select(BuildOccurrence).where(
                BuildOccurrence.tenant_id == self.context.tenant_id,
                BuildOccurrence.request_id == request_id,
            )
        ).one_or_none()
        if prior is not None:
            if prior.request_digest != digest:
                raise DomainError("idempotency_conflict", "idempotency_conflict")
            return prior
        existing = self.session.exec(
            select(BuildOccurrence).where(
                BuildOccurrence.tenant_id == self.context.tenant_id,
                BuildOccurrence.optimizer_id == optimizer.id,
                BuildOccurrence.plan_row_id == row.id,
                BuildOccurrence.strategy_version_id == resolved_strategy,
                BuildOccurrence.scheduled_at == scheduled_at,
            )
        ).one_or_none()
        if existing is not None:
            return existing
        occurrence = BuildOccurrence(
            tenant_id=self.context.tenant_id,
            optimizer_id=optimizer.id,
            plan_row_id=row.id,
            request_id=request_id,
            request_digest=digest,
            strategy_version_id=resolved_strategy,
            bc_id=version.bc_id,
            advertiser_ids=list(version.advertiser_ids),
            provider_connection_id=row.provider_connection_id,
            application_id=row.application_id,
            title=row.title,
            promotion_url=row.promotion_url,
            external_drama_id=row.external_drama_id,
            order_materials=row.order_materials,
            scheduled_at=scheduled_at,
            evidence={"snapshot": payload},
        )
        self.session.add(occurrence)
        self.session.flush()
        occurrence.dispatch_id = enqueue_after_commit(
            self.session,
            context=self.context,
            task_name=TASK_NAME,
            task_key=f"ai-build-occurrence:{occurrence.id}",
            payload={"occurrence_id": str(occurrence.id)},
        )
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    def dispatch_occurrence(self, occurrence_id: UUID) -> BuildOccurrence:
        """Advance one occurrence through local draft/preview/submission stages."""
        self._authorize()
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(
                BuildOccurrence.tenant_id == self.context.tenant_id,
                BuildOccurrence.id == occurrence_id,
            )
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            raise DomainError("ai_build_occurrence_not_found", "ai_build_occurrence_not_found")
        if occurrence.status in TERMINAL:
            # UNKNOWN is deliberately terminal until an explicit reconciliation
            # changes evidence; a retry must never create a second submission.
            return occurrence
        try:
            if occurrence.draft_id is None:
                draft_request = uuid5(OCCURRENCE_NAMESPACE, f"draft:{occurrence.id}")
                occurrence.draft_id = drafts.create_draft(
                    self.session,
                    context=self.context,
                    bc_id=occurrence.bc_id,
                    strategy_version_id=occurrence.strategy_version_id,
                    provider_connection_id=occurrence.provider_connection_id,
                    execution_connection_id=None,
                    application_id=occurrence.application_id,
                    drama_lines=[occurrence.title],
                    account_lines=list(occurrence.advertiser_ids),
                    link_config={},
                    manual_links=(
                        [
                            {
                                "line_no": 1,
                                "url": occurrence.promotion_url,
                                "material_identifier": occurrence.title,
                                "external_drama_id": occurrence.external_drama_id or "",
                                "protected_base": "",
                            }
                        ]
                        if occurrence.promotion_url
                        else []
                    ),
                    request_id=draft_request,
                )
            draft_request = uuid5(OCCURRENCE_NAMESPACE, f"prepare:{occurrence.id}")
            drafts.prepare_draft(
                self.session,
                context=self.context,
                draft_id=occurrence.draft_id,
                request_id=draft_request,
            )
            preview_id = occurrence.preview_id
            if preview_id is None:
                draft = drafts.get_draft(self.session, context=self.context, draft_id=occurrence.draft_id)
                preview_id = previews.generate_preview(
                    self.session,
                    context=self.context,
                    draft_id=occurrence.draft_id,
                    expected_revision=draft.revision,
                )
                occurrence.preview_id = preview_id
            preview = self.session.get(BuildPreview, occurrence.preview_id)
            if preview is None or preview.status not in {"FROZEN"}:
                occurrence.status = "WAITING_MATERIAL"
                occurrence.evidence["preview_status"] = getattr(preview, "status", "BUILDING")
            else:
                receipt = submissions.submit_preview(
                    self.session,
                    context=self.context,
                    preview_id=occurrence.preview_id,
                    request_id=uuid5(OCCURRENCE_NAMESPACE, f"submit:{occurrence.id}"),
                )
                occurrence.submission_id = receipt.submission_id
                occurrence.status = "DISPATCHED"
            occurrence.error_code = None
        except DomainError as error:
            occurrence.error_code = error.code
            occurrence.status = "WAITING_MATERIAL" if error.code in MATERIAL_WAIT_ERRORS else "FAILED"
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    def reconcile_occurrence(
        self,
        occurrence_id: UUID,
        *,
        state: str,
        evidence: dict[str, Any] | None = None,
    ) -> BuildOccurrence:
        """Import an existing build readback without replaying unknown work."""
        if state not in {"PARTIAL", "UNKNOWN", "COMPLETE", "FAILED", "DISPATCHED"}:
            raise DomainError("ai_build_occurrence_state_invalid", "ai_build_occurrence_state_invalid")
        self._authorize()
        occurrence = self.session.exec(
            select(BuildOccurrence)
            .where(BuildOccurrence.tenant_id == self.context.tenant_id, BuildOccurrence.id == occurrence_id)
            .with_for_update()
        ).one_or_none()
        if occurrence is None:
            raise DomainError("ai_build_occurrence_not_found", "ai_build_occurrence_not_found")
        occurrence.status = state
        if evidence:
            occurrence.evidence = {**occurrence.evidence, "readback": evidence}
        occurrence.updated_at = datetime.now(UTC)
        self.session.add(occurrence)
        self.session.flush()
        return occurrence

    # A concise alias used by result workers and tests.
    reconcile = reconcile_occurrence

