"""Tenant/optimizer-scoped DSH proposals and publication decisions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.models import (
    AIOptimizerRuleBinding,
    AIOptimizerStrategyBinding,
    AIOptimizerVersion,
)
from app.modules.ai_workbench.reviews import (
    DSHReviewTask,
    authorize,
    frozen_evidence_ids,
    load_optimizer,
)
from app.modules.automation_rules.models import AutomationRule
from app.modules.automation_rules.schemas import RuleDefinition, RuleUpdate
from app.modules.strategies.models import Strategy, StrategyVersion
from app.modules.strategies.schemas import StrategyConfig


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["strategy", "rule"]
    target_id: UUID | None = None
    after: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    evidence_ids: tuple[UUID, ...] = ()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    evidence_ids: tuple[UUID, ...] = ()
    knowledge_refs: list[dict[str, Any]] = Field(default_factory=list)
    proposals: tuple[ProposalInput, ...] = ()
    authorization_ttl_seconds: int = Field(default=300, ge=30, le=900)
    dsh_task_id: str | None = Field(default=None, max_length=160)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    reason: str | None = None


class AIProposal(SQLModel, table=True):
    __tablename__ = "ai_optimizer_proposal"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_ai_proposal_request"),
        ForeignKeyConstraint(
            ["tenant_id", "optimizer_id"],
            ["ai_optimizer.tenant_id", "ai_optimizer.id"],
            name="fk_ai_proposal_optimizer",
        ),
        CheckConstraint("kind IN ('strategy','rule')", name="ck_ai_proposal_kind"),
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED','STALE')",
            name="ck_ai_proposal_status",
        ),
    )
    id: UUID = SQLField(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = SQLField(foreign_key="tenant.id")
    optimizer_id: UUID
    review_task_id: UUID = SQLField(foreign_key="ai_dsh_review_task.id")
    kind: str = SQLField(max_length=16)
    target_id: UUID | None = None
    status: str = SQLField(default="PENDING", max_length=16)
    before: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    after: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    evidence: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    authorization: dict[str, Any] = SQLField(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    base_optimizer_version: int
    base_strategy_version_id: UUID | None = None
    base_rule_version: int | None = None
    base_rule_id: UUID | None = None
    published_version_id: UUID | None = None
    decision_by: UUID | None = None
    decision_reason: str | None = SQLField(default=None, max_length=1000)
    decision_request_id: UUID | None = None
    decision_request_digest: str | None = SQLField(default=None, max_length=64)
    decision_at: datetime | None = SQLField(
        default=None, sa_type=DateTime(timezone=True)
    )
    request_id: UUID
    created_by: UUID = SQLField(foreign_key="user.id")
    created_at: datetime = SQLField(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )


def _public(row: AIProposal) -> dict[str, Any]:
    names = (
        "id",
        "tenant_id",
        "optimizer_id",
        "review_task_id",
        "kind",
        "target_id",
        "status",
        "before",
        "after",
        "evidence",
        "authorization",
        "base_optimizer_version",
        "base_strategy_version_id",
        "base_rule_id",
        "base_rule_version",
        "published_version_id",
        "decision_by",
        "decision_reason",
        "decision_at",
        "request_id",
        "created_at",
    )
    return {name: getattr(row, name) for name in names}


def list_proposals(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    status: str | None = None,
) -> list[dict[str, Any]]:
    authorize(session, context)
    query = (
        select(AIProposal)
        .where(
            AIProposal.tenant_id == context.tenant_id,
            AIProposal.optimizer_id == optimizer_id,
        )
        .order_by(col(AIProposal.created_at).desc())
    )
    if status is not None:
        query = query.where(AIProposal.status == status)
    return [_public(row) for row in session.exec(query).all()]


def _limit_value(limits: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in limits:
            return limits[key]
    return None


def _authorized(limits: Mapping[str, Any], kind: str, mode: str) -> bool:
    """Read both the compact and nested authorization-limit contracts."""
    raw = _limit_value(limits, kind, f"allow_{kind}", f"{kind}_publication")
    if isinstance(raw, dict):
        raw = raw.get("allowed", raw.get("allow", raw.get("enabled")))
    if raw is None:
        # MANUAL means an operator supplies the publication decision.  It still
        # gets a pending proposal and the scope is checked again at accept time;
        # an empty limit therefore does not silently turn into AUTO authority.
        return mode == "MANUAL"
    return raw is True


def _within_authorization(
    *,
    version: AIOptimizerVersion,
    kind: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[bool, str | None]:
    limits = version.authorization_limits or {}
    if not isinstance(limits, Mapping):
        raise DomainError(
            "ai_proposal_invalid", "authorization limits must be an object"
        )
    if kind == "strategy":
        if not _authorized(limits, "strategy", version.optimization_mode):
            return False, "strategy_publication_not_authorized"
        before_cfg = before.get("config", before) if isinstance(before, Mapping) else {}
        after_cfg = after.get("config", after) if isinstance(after, Mapping) else {}
        if not isinstance(before_cfg, Mapping) or not isinstance(after_cfg, Mapping):
            raise DomainError("ai_proposal_invalid", "strategy scope must be an object")
        nested = (
            limits.get("strategy") if isinstance(limits.get("strategy"), dict) else {}
        )
        for field in ("budget", "daily_spend", "spend"):
            if field in after_cfg and field in before_cfg:
                maximum = _limit_value(limits, f"max_{field}_delta", f"{field}_delta")
                maximum = (
                    maximum
                    if maximum is not None
                    else _limit_value(nested, f"max_{field}_delta", f"{field}_delta")
                )
                if maximum is None:
                    return False, f"{field}_delta_out_of_scope"
                try:
                    delta = abs(
                        Decimal(str(after_cfg[field])) - Decimal(str(before_cfg[field]))
                    )
                    maximum_decimal = Decimal(str(maximum))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise DomainError(
                        "ai_proposal_invalid", f"{field} delta must be numeric"
                    ) from exc
                if delta > maximum_decimal:
                    return False, f"{field}_delta_out_of_scope"
    else:
        if not _authorized(limits, "rule", version.optimization_mode):
            return False, "rule_publication_not_authorized"
        if not isinstance(version.bc_id, str) or not isinstance(
            version.advertiser_ids, (list, tuple, set)
        ):
            raise DomainError("ai_invalid_rule_scope", "optimizer scope is malformed")
        if not all(isinstance(item, str) for item in version.advertiser_ids):
            raise DomainError(
                "ai_invalid_rule_scope", "optimizer advertiser scope is malformed"
            )
        if not isinstance(after, Mapping):
            raise DomainError("ai_invalid_rule_scope", "rule scope must be an object")
        definition = after.get("definition", after)
        if not isinstance(definition, Mapping):
            raise DomainError(
                "ai_invalid_rule_scope", "rule definition scope must be an object"
            )
        accounts = definition.get("accounts")
        # Older proposal payloads placed the account IDs directly under the
        # definition (or omitted ``mode`` inside ``accounts``).  Normalize
        # those unambiguous, bounded shapes before applying the strict scope
        # checks; an empty/broad shape still fails closed below.
        if accounts is None:
            legacy = {
                key: definition[key]
                for key in ("bc_ids", "advertiser_ids")
                if key in definition
            }
            if "advertiser_ids" in legacy:
                legacy["mode"] = "ACCOUNTS"
            elif "bc_ids" in legacy:
                legacy["mode"] = "BC"
            accounts = legacy
        elif isinstance(accounts, Mapping) and "mode" not in accounts:
            accounts = dict(accounts)
            if "advertiser_ids" in accounts:
                accounts["mode"] = "ACCOUNTS"
            elif "bc_ids" in accounts:
                accounts["mode"] = "BC"
        if not isinstance(accounts, Mapping):
            raise DomainError(
                "ai_invalid_rule_scope", "nested rule accounts scope must be an object"
            )
        mode = accounts.get("mode")
        if mode not in {"ALL", "BC", "ACCOUNTS"}:
            raise DomainError("ai_invalid_rule_scope", "account scope mode is required")
        advertiser_ids = accounts.get("advertiser_ids")
        bc_ids = accounts.get("bc_ids")
        if advertiser_ids is not None and (
            not isinstance(advertiser_ids, (list, tuple, set))
            or not all(isinstance(item, str) for item in advertiser_ids)
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "advertiser scope must be a list"
            )
        if bc_ids is not None and (
            not isinstance(bc_ids, (list, tuple, set))
            or not all(isinstance(item, str) for item in bc_ids)
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "business center scope must be a list"
            )
        advertiser_scope = set(advertiser_ids or ())
        bc_scope = set(bc_ids or ())
        if mode == "ALL":
            # ALL ignores account IDs at execution time and would therefore
            # grant an AUTO proposal a broader scope than the optimizer.
            return False, "account_scope_not_authorized"
        if mode == "BC" and not bc_scope:
            raise DomainError("ai_invalid_rule_scope", "bc scope requires bc_ids")
        if mode == "ACCOUNTS" and not advertiser_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "account scope requires advertiser_ids"
            )
        if mode == "BC" and advertiser_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "bc scope cannot include advertiser_ids"
            )
        if mode == "ACCOUNTS" and bc_scope:
            raise DomainError(
                "ai_invalid_rule_scope", "account scope cannot include bc_ids"
            )
        if advertiser_scope and not advertiser_scope.issubset(
            set(version.advertiser_ids)
        ):
            return False, "advertiser_scope_out_of_scope"
        if mode == "BC" and bc_scope != {version.bc_id}:
            return False, "bc_scope_out_of_scope"
        if definition.get("bc_id") is not None and not isinstance(
            definition["bc_id"], str
        ):
            raise DomainError(
                "ai_invalid_rule_scope", "business center scope must be a string"
            )
        if definition.get("bc_id") is not None and definition["bc_id"] != version.bc_id:
            return False, "bc_scope_out_of_scope"
    return True, None


def _strategy_baseline(
    session: Session,
    *,
    tenant_id: UUID,
    optimizer_id: UUID,
    version: AIOptimizerVersion,
) -> StrategyVersion:
    binding = session.exec(
        select(AIOptimizerStrategyBinding).where(
            AIOptimizerStrategyBinding.tenant_id == tenant_id,
            AIOptimizerStrategyBinding.optimizer_id == optimizer_id,
            AIOptimizerStrategyBinding.id == version.strategy_binding_id,
            AIOptimizerStrategyBinding.active.is_(True),
        )
    ).one_or_none()
    if binding is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    row = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.tenant_id == tenant_id,
            StrategyVersion.id == binding.strategy_version_id,
        )
    ).one_or_none()
    if row is None:
        raise DomainError(
            "ai_strategy_binding_required", "ai_strategy_binding_required"
        )
    return row


def create_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    review_task: DSHReviewTask,
    change: ProposalInput,
    request_id: UUID | None = None,
) -> AIProposal:
    authorize(session, context, write=True)
    if (
        review_task.tenant_id != context.tenant_id
        or review_task.optimizer_id != optimizer_id
    ):
        raise DomainError(
            "ai_review_scope_mismatch", "review task is outside optimizer scope"
        )
    optimizer, version = load_optimizer(session, context, optimizer_id)
    if review_task.optimizer_version != version.number:
        raise DomainError("ai_proposal_stale", "review task optimizer version is stale")
    frozen_ids = frozen_evidence_ids(review_task.evidence)
    if any(str(item) not in frozen_ids for item in change.evidence_ids):
        raise DomainError(
            "ai_evidence_not_found",
            "proposal evidence is outside the frozen review task",
        )
    request_id = request_id or uuid4()
    existing = session.exec(
        select(AIProposal).where(
            AIProposal.tenant_id == context.tenant_id,
            AIProposal.request_id == request_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.optimizer_id != optimizer_id:
            raise DomainError(
                "ai_proposal_scope_mismatch",
                "request is already used by another optimizer",
            )
        return existing
    if change.kind == "strategy":
        baseline = _strategy_baseline(
            session,
            tenant_id=context.tenant_id,
            optimizer_id=optimizer_id,
            version=version,
        )
        before = {
            "version_id": str(baseline.id),
            "version": baseline.number,
            "config": baseline.config,
        }
        base_strategy_version_id, base_rule_id, base_rule_version = (
            baseline.id,
            None,
            None,
        )
    else:
        if change.target_id is None:
            raise DomainError("ai_proposal_target_required", "rule target is required")
        binding = session.exec(
            select(AIOptimizerRuleBinding).where(
                AIOptimizerRuleBinding.tenant_id == context.tenant_id,
                AIOptimizerRuleBinding.optimizer_id == optimizer_id,
                AIOptimizerRuleBinding.rule_id == change.target_id,
                AIOptimizerRuleBinding.active.is_(True),
            )
        ).one_or_none()
        rule = session.exec(
            select(AutomationRule).where(
                AutomationRule.tenant_id == context.tenant_id,
                AutomationRule.id == change.target_id,
            )
        ).one_or_none()
        if binding is None or rule is None:
            raise DomainError(
                "ai_rule_not_bound", "rule is not assigned to this optimizer"
            )
        before = {
            "rule_id": str(rule.id),
            "version": rule.version,
            "name": rule.name,
            "enabled": rule.enabled,
            "definition": rule.definition,
        }
        base_strategy_version_id, base_rule_id, base_rule_version = (
            None,
            rule.id,
            rule.version,
        )
    allowed, reason = _within_authorization(
        version=version, kind=change.kind, before=before, after=change.after
    )
    row = AIProposal(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        review_task_id=review_task.id,
        kind=change.kind,
        target_id=change.target_id,
        status="PENDING",
        before=sanitize_public(before),
        after=sanitize_public(change.after),
        evidence={
            "evidence_ids": [str(item) for item in change.evidence_ids],
            "review_task_id": str(review_task.id),
            "captured_optimizer_version": version.number,
        },
        authorization={
            "allowed": allowed,
            "reason": reason,
            "mode": version.optimization_mode,
            "checked_at": datetime.now(UTC).isoformat(),
        },
        base_optimizer_version=version.number,
        base_strategy_version_id=base_strategy_version_id,
        base_rule_version=base_rule_version,
        base_rule_id=base_rule_id,
        request_id=request_id,
        created_by=context.actor_id,
    )
    session.add(row)
    session.flush()
    return row


def _mark(
    row: AIProposal, *, context: TenantContext, status: str, reason: str | None = None
) -> AIProposal:
    row.status, row.decision_by, row.decision_reason, row.decision_at = (
        status,
        context.actor_id,
        reason,
        datetime.now(UTC),
    )
    return row


def _publish_strategy(
    session: Session, *, context: TenantContext, proposal: AIProposal
) -> UUID:
    from app.modules.ai_workbench import service as optimizer_service
    from app.modules.strategies import service as strategy_service

    optimizer, current = load_optimizer(session, context, proposal.optimizer_id)
    baseline = _strategy_baseline(
        session, tenant_id=context.tenant_id, optimizer_id=optimizer.id, version=current
    )
    if baseline.id != proposal.base_strategy_version_id:
        raise DomainError("ai_proposal_stale", "strategy base version is stale")
    strategy = session.exec(
        select(Strategy)
        .where(
            Strategy.tenant_id == context.tenant_id, Strategy.id == baseline.strategy_id
        )
        .with_for_update()
    ).one()
    refs = session.exec(
        select(AIOptimizerStrategyBinding).where(
            AIOptimizerStrategyBinding.tenant_id == context.tenant_id,
            AIOptimizerStrategyBinding.strategy_version_id == baseline.id,
            AIOptimizerStrategyBinding.active.is_(True),
        )
    ).all()
    if any(ref.optimizer_id != optimizer.id for ref in refs):
        raise DomainError(
            "ai_strategy_shared", "shared strategy must be copied and owned first"
        )
    try:
        config = StrategyConfig.model_validate(
            proposal.after.get("config", proposal.after)
        )
    except Exception as exc:
        raise DomainError(
            "ai_proposal_invalid", "strategy proposal is invalid"
        ) from exc
    new_version_id = strategy_service.append_version(
        session,
        context=context,
        strategy_id=strategy.id,
        config=config,
        expected_version=baseline.number,
        request_id=uuid4(),
    )
    # Optimizer versions are immutable.  Use the existing append-version
    # service to publish a new optimizer snapshot pointing at the new strategy
    # version instead of mutating the snapshot captured by the review task.
    from app.modules.ai_workbench.schemas import OptimizerUpdate

    result = optimizer_service.update_optimizer(
        session,
        context=context,
        optimizer_id=optimizer.id,
        body=OptimizerUpdate(strategy_version_id=new_version_id, request_id=uuid4()),
    )
    return result.strategy_version_id


def _publish_rule(
    session: Session, *, context: TenantContext, proposal: AIProposal
) -> UUID:
    from app.modules.automation_rules import service as rule_service

    _, _ = load_optimizer(session, context, proposal.optimizer_id)
    rule_id = proposal.base_rule_id or proposal.target_id
    if rule_id is None:
        raise DomainError("ai_proposal_target_required", "rule target is required")
    rule = session.exec(
        select(AutomationRule)
        .where(
            AutomationRule.tenant_id == context.tenant_id, AutomationRule.id == rule_id
        )
        .with_for_update()
    ).one_or_none()
    if rule is None or rule.version != proposal.base_rule_version:
        raise DomainError("ai_proposal_stale", "rule base version is stale")
    other = session.exec(
        select(AIOptimizerRuleBinding).where(
            AIOptimizerRuleBinding.tenant_id == context.tenant_id,
            AIOptimizerRuleBinding.rule_id == rule_id,
            AIOptimizerRuleBinding.optimizer_id != proposal.optimizer_id,
            AIOptimizerRuleBinding.active.is_(True),
        )
    ).first()
    if other is not None:
        raise DomainError(
            "ai_rule_shared", "shared rule must be copied and owned first"
        )
    try:
        definition = RuleDefinition.model_validate(
            proposal.after.get("definition", proposal.after)
        )
        body = RuleUpdate(
            name=proposal.after.get("name", rule.name),
            enabled=proposal.after.get("enabled", rule.enabled),
            definition=definition,
            expected_version=rule.version,
        )
    except Exception as exc:
        raise DomainError("ai_proposal_invalid", "rule proposal is invalid") from exc
    rule_service.update_rule(session, context=context, rule_id=rule.id, body=body)
    return rule.id


def _decision_digest(action: str, request_id: UUID, reason: str | None) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            {"action": action, "request_id": str(request_id), "reason": reason},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def accept_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    proposal_id: UUID,
    request_id: UUID | None = None,
) -> AIProposal:
    authorize(session, context, write=True)
    row = session.exec(
        select(AIProposal)
        .where(
            AIProposal.tenant_id == context.tenant_id,
            AIProposal.optimizer_id == optimizer_id,
            AIProposal.id == proposal_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise DomainError("ai_proposal_not_found", "ai_proposal_not_found")
    request_id = request_id or uuid4()
    digest = _decision_digest("accept", request_id, None)
    if row.status in {"ACCEPTED", "REJECTED", "STALE"}:
        if row.decision_request_digest != digest:
            raise DomainError(
                "ai_decision_conflict",
                "proposal decision retry conflicts with the original request",
            )
        return row
    row.decision_request_id = request_id
    row.decision_request_digest = digest
    _, current = load_optimizer(session, context, optimizer_id)
    if current.number != row.base_optimizer_version:
        return _mark(
            row,
            context=context,
            status="STALE",
            reason="optimizer_base_version_changed",
        )
    if row.authorization.get("allowed") is not True:
        raise DomainError(
            "ai_publication_not_authorized",
            row.authorization.get("reason", "publication is outside authorization"),
        )
    try:
        published = (
            _publish_strategy(session, context=context, proposal=row)
            if row.kind == "strategy"
            else _publish_rule(session, context=context, proposal=row)
        )
    except DomainError as exc:
        if exc.code in {
            "ai_proposal_stale",
            "version_conflict",
            "automation_rule_version_conflict",
        }:
            return _mark(row, context=context, status="STALE", reason=exc.code)
        raise
    row.published_version_id = published
    _mark(row, context=context, status="ACCEPTED")
    session.flush()
    return row


def reject_proposal(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    proposal_id: UUID,
    reason: str | None = None,
    request_id: UUID | None = None,
) -> AIProposal:
    authorize(session, context, write=True)
    row = session.exec(
        select(AIProposal)
        .where(
            AIProposal.tenant_id == context.tenant_id,
            AIProposal.optimizer_id == optimizer_id,
            AIProposal.id == proposal_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise DomainError("ai_proposal_not_found", "ai_proposal_not_found")
    request_id = request_id or uuid4()
    digest = _decision_digest("reject", request_id, reason)
    if row.status in {"ACCEPTED", "REJECTED", "STALE"}:
        if row.decision_request_digest != digest:
            raise DomainError(
                "ai_decision_conflict",
                "proposal decision retry conflicts with the original request",
            )
        return row
    row.decision_request_id = request_id
    row.decision_request_digest = digest
    _mark(row, context=context, status="REJECTED", reason=reason)
    session.flush()
    return row


def publish_auto_proposals(
    session: Session,
    *,
    context: TenantContext,
    optimizer_id: UUID,
    review_task_id: UUID,
) -> list[AIProposal]:
    _, version = load_optimizer(session, context, optimizer_id)
    if version.optimization_mode != "AUTO":
        return []
    rows = session.exec(
        select(AIProposal)
        .where(
            AIProposal.tenant_id == context.tenant_id,
            AIProposal.optimizer_id == optimizer_id,
            AIProposal.review_task_id == review_task_id,
            AIProposal.status == "PENDING",
        )
        .order_by(col(AIProposal.created_at))
    ).all()
    return [
        accept_proposal(
            session,
            context=context,
            optimizer_id=optimizer_id,
            proposal_id=row.id,
            request_id=row.request_id,
        )
        for row in rows
        if row.authorization.get("allowed") is True
    ]
