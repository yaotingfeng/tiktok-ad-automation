"""Immutable evidence snapshots for optimizer rule decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.modules.ai_workbench.rule_binding import RuleBindingScope
from app.modules.automation_rules.evaluate import Evaluation
from app.modules.automation_rules.models import (
    AutomationRule,
    AutomationRuleEvidence,
    AutomationRuleRun,
)
from app.modules.automation_rules.schemas import RuleDefinition


@dataclass(frozen=True)
class RuleEvidenceSnapshot:
    tenant_id: UUID
    rule_id: UUID
    rule_version: int
    definition: dict[str, Any]
    optimizer_id: UUID | None = None
    optimizer_version: int | None = None
    binding_version: int | None = None
    binding_id: UUID | None = None
    bc_id: str | None = None
    advertiser_ids: tuple[str, ...] = ()
    authorization_revision: int | None = None
    checked_metrics: dict[str, Any] = field(default_factory=dict)
    data_freshness: dict[str, Any] = field(default_factory=dict)
    publication_versions: dict[str, Any] = field(default_factory=dict)
    skipped_accounts: tuple[dict[str, Any], ...] = ()
    checked_objects: tuple[dict[str, Any], ...] = ()
    hits: tuple[dict[str, Any], ...] = ()
    action_ledger_refs: tuple[str, ...] = ()
    management_tasks: tuple[dict[str, Any], ...] = ()
    readback_status: dict[str, Any] = field(default_factory=dict)
    retention_until: datetime | None = None

    @property
    def rule_definition(self) -> dict[str, Any]:
        """Compatibility/readability alias used by evidence consumers."""
        return self.definition

    @property
    def optimizer_scope(self) -> dict[str, Any]:
        return {
            "optimizer_id": str(self.optimizer_id) if self.optimizer_id else None,
            "optimizer_version": self.optimizer_version,
            "bc_id": self.bc_id,
            "advertiser_ids": list(self.advertiser_ids),
            "authorization_revision": self.authorization_revision,
        }

    @classmethod
    def from_evaluation(
        cls,
        *,
        rule: AutomationRule,
        run: AutomationRuleRun,
        result: Evaluation,
        skipped_accounts: list[dict[str, str]],
        scope: RuleBindingScope | None = None,
    ) -> RuleEvidenceSnapshot:
        definition = RuleDefinition.model_validate(rule.definition)
        checked_metrics = {
            f"{condition.metric}:{condition.range}" if condition.range else condition.metric: list(condition.values)
            for condition in definition.conditions
        }
        hits = tuple(
            {
                "subject_key": hit.subject_key,
                "subject_label": hit.subject_label,
                "advertiser_id": hit.advertiser_id,
                "bc_id": hit.bc_id,
                "metrics": hit.metrics,
                "target_count": hit.target_count,
                "targets": [asdict(target) for target in hit.targets],
            }
            for hit in result.hits
        )
        checked_objects = tuple(
            {
                "key": target.key,
                "kind": target.kind,
                "advertiser_id": target.advertiser_id,
                "bc_id": target.bc_id,
            }
            for hit in result.hits
            for target in hit.targets
        )
        return cls(
            tenant_id=rule.tenant_id,
            rule_id=rule.id,
            rule_version=run.rule_version,
            definition=dict(rule.definition),
            optimizer_id=scope.optimizer_id if scope else run.optimizer_id,
            optimizer_version=scope.optimizer_version if scope else run.optimizer_version,
            binding_version=scope.optimizer_version if scope else run.binding_version,
            binding_id=scope.binding_id if scope else run.binding_id,
            bc_id=scope.bc_id if scope else None,
            advertiser_ids=scope.advertiser_ids if scope else (),
            authorization_revision=scope.authorization_revision if scope else run.authorization_revision,
            checked_metrics=checked_metrics,
            data_freshness={
                "evaluated_at": run.finished_at.isoformat()
                if run.finished_at
                else datetime.now(UTC).isoformat(),
                "checked_count": result.checked,
            },
            skipped_accounts=tuple(skipped_accounts),
            checked_objects=checked_objects,
            hits=hits,
            retention_until=datetime.now(UTC) + timedelta(days=365),
        )

    def persist(
        self,
        session: Session,
        *,
        run_id: UUID | None = None,
    ) -> AutomationRuleEvidence:
        row = AutomationRuleEvidence(
            tenant_id=self.tenant_id,
            run_id=run_id,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            definition=self.definition,
            optimizer_id=self.optimizer_id,
            optimizer_version=self.optimizer_version,
            binding_version=self.binding_version,
            binding_id=self.binding_id,
            bc_id=self.bc_id,
            advertiser_ids=list(self.advertiser_ids),
            authorization_revision=self.authorization_revision,
            checked_metrics=self.checked_metrics,
            data_freshness=self.data_freshness,
            publication_versions=self.publication_versions,
            skipped_accounts=list(self.skipped_accounts),
            checked_objects=list(self.checked_objects),
            hits=list(self.hits),
            action_ledger_refs=list(self.action_ledger_refs),
            management_tasks=list(self.management_tasks),
            readback_status=self.readback_status,
            retention_until=self.retention_until or (datetime.now(UTC) + timedelta(days=365)),
        )
        session.add(row)
        session.flush()
        return row


def persist_evidence(
    session: Session,
    *,
    rule: AutomationRule,
    run: AutomationRuleRun,
    result: Evaluation,
    skipped_accounts: list[dict[str, str]],
    scope: RuleBindingScope | None = None,
) -> AutomationRuleEvidence:
    snapshot = RuleEvidenceSnapshot.from_evaluation(
        rule=rule, run=run, result=result, skipped_accounts=skipped_accounts, scope=scope
    )
    row = snapshot.persist(session, run_id=run.id)
    run.evidence_id = row.id
    return row
