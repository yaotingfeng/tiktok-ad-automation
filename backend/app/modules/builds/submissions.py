"""Idempotent submission and bounded local expansion; callers own transactions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.access import resolve_account_access
from app.modules.accounts.models import TenantBC
from app.modules.builds.execution_models import (
    DraftUnitReservation,
    ExecutionStep,
    Submission,
    SubmissionRequest,
    SubmissionUnit,
)
from app.modules.builds.execution_schemas import (
    ExecutionUnit,
    StepClaim,
    StepPublic,
    SubmissionReceipt,
    SubmissionUnitPublic,
    SubmissionView,
)
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from app.modules.builds.previews import load_frozen_unit
from app.modules.tenants.permissions import require_tenant


def authorize(session: Session, context: TenantContext, action: str = "read") -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action
    )


def submission_row(
    session: Session, context: TenantContext, identity: UUID, *, lock: bool = False
) -> Submission:
    stmt = select(Submission).where(
        Submission.tenant_id == context.tenant_id, Submission.id == identity
    )
    if lock:
        stmt = stmt.with_for_update(key_share=True)
    row = session.exec(stmt.execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise DomainError("resource_not_found", "提交不存在")
    return row


# All earlier eligible frozen scopes count, even before their workers expand.
# An excluded pair never reserves scope; a later task cannot steal an older one.
COVERED = """EXISTS (
 SELECT 1 FROM build_submission old JOIN build_unit old_u
 ON old_u.tenant_id=old.tenant_id AND old_u.preview_id=old.preview_id
 WHERE old.tenant_id=:tenant AND old.draft_id=:draft AND old.ordinal<:ordinal
 AND old_u.drama_id=u.drama_id AND old_u.advertiser_id=u.advertiser_id
 AND old_u.complete AND old_u.readiness IN ('READY','PREPARING'))"""


def params(row: Submission) -> dict[str, Any]:
    return {
        "tenant": row.tenant_id,
        "submission": row.id,
        "preview": row.preview_id,
        "draft": row.draft_id,
        "ordinal": row.ordinal,
        "unit": None,
    }


def submit_preview(
    session: Session, *, context: TenantContext, preview_id: UUID, request_id: UUID
) -> SubmissionReceipt:
    authorize(session, context, "build")
    # A request can race across different draft locks. Serialize exactly that
    # tenant-owned ledger key before reading either parent; no external call.
    SQLAlchemySession.execute(
        session,
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"submission:{context.tenant_id}:{request_id}"},
    )
    alias = session.get(
        SubmissionRequest, (context.tenant_id, request_id), populate_existing=True
    )
    if alias is not None:
        if alias.preview_id != preview_id:
            raise DomainError("idempotency_conflict", "幂等键已绑定其他预览")
        saved = submission_row(session, context, alias.submission_id)
        return SubmissionReceipt(submission_id=saved.id, status=saved.status)
    preview = session.exec(
        select(BuildPreview).where(
            BuildPreview.tenant_id == context.tenant_id, BuildPreview.id == preview_id
        )
    ).one_or_none()
    if preview is None:
        raise DomainError("preview_not_found", "预览不存在")
    draft = session.exec(
        select(BuildDraft)
        .where(
            BuildDraft.tenant_id == context.tenant_id, BuildDraft.id == preview.draft_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    preview = session.exec(
        select(BuildPreview)
        .where(BuildPreview.id == preview_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    existing = session.exec(
        select(Submission).where(
            Submission.tenant_id == context.tenant_id,
            Submission.preview_id == preview_id,
        )
    ).one_or_none()
    if existing is None:
        bc = session.get(
            TenantBC, (context.tenant_id, preview.bc_id), populate_existing=True
        )
        if bc is None or bc.ownership_conflict:
            raise DomainError("account_ownership_conflict", "BC 不可提交")
        if preview.status != "FROZEN" or not preview.content_digest:
            raise DomainError("preview_not_frozen", "预览尚未冻结")
        if draft.revision != preview.draft_revision:
            raise DomainError("draft_revision_conflict", "草稿已变更")
        eligible = session.exec(
            select(BuildUnit.id)
            .where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == preview_id,
                col(BuildUnit.complete).is_(True),
                col(BuildUnit.readiness).in_(["READY", "PREPARING"]),
            )
            .limit(1)
        ).first()
        if eligible is None:
            raise DomainError("preview_not_submittable", "没有可提交组合")
        ordinal = (
            session.exec(
                select(func.coalesce(func.max(Submission.ordinal), 0)).where(
                    Submission.tenant_id == context.tenant_id,
                    Submission.draft_id == draft.id,
                )
            ).one()
            + 1
        )
        remaining = SQLAlchemySession.execute(
            session,
            text(
                "SELECT u.id FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview AND u.complete AND u.readiness IN ('READY','PREPARING') AND NOT "
                + COVERED
                + " LIMIT 1"
            ),
            {
                "tenant": context.tenant_id,
                "preview": preview.id,
                "draft": draft.id,
                "ordinal": ordinal,
            },
        ).first()
        if remaining is None:
            raise DomainError("preview_not_submittable", "没有尚未提交的可执行组合")
        existing = Submission(
            tenant_id=context.tenant_id,
            bc_id=preview.bc_id,
            preview_id=preview.id,
            draft_id=draft.id,
            actor_id=context.actor_id,
            ordinal=ordinal,
        )
        session.add(existing)
        session.flush()
        # Permission changes after this point are checked again per bounded unit
        # and before every effect. No task reads mutable draft execution inputs.
        from app.modules.builds.submission_tasks import queue_expansion

        queue_expansion(session, existing)
    session.add(
        SubmissionRequest(
            tenant_id=context.tenant_id,
            request_id=request_id,
            submission_id=existing.id,
            preview_id=existing.preview_id,
            bc_id=existing.bc_id,
        )
    )
    session.flush()
    return SubmissionReceipt(submission_id=existing.id, status=existing.status)


BLUEPRINTS = """
WITH candidates AS (
 SELECT 0 priority, 'MATERIAL:'||m.material_id k, NULL::text parent,
 'MATERIAL' kind, NULL::uuid group_id, NULL::uuid ad_id, m.material_id
 FROM preview_group_material m WHERE m.tenant_id=:tenant AND m.preview_id=:preview AND m.drama_id=:drama
 UNION ALL SELECT 1,'CTA',NULL,'CTA',NULL,NULL,NULL
 UNION ALL SELECT 2,'CAMPAIGN',NULL,'CAMPAIGN',NULL,NULL,NULL
 UNION ALL SELECT 3,'READBACK:CAMPAIGN','CAMPAIGN','READBACK',NULL,NULL,NULL
 UNION ALL SELECT 4,'ADGROUP:'||g.id,'CAMPAIGN','ADGROUP',g.id,NULL,NULL
 FROM planned_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit
 UNION ALL SELECT 5,'READBACK:ADGROUP:'||g.id,'ADGROUP:'||g.id,'READBACK',g.id,NULL,NULL
 FROM planned_group g WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit
 UNION ALL SELECT 6,'AD:'||a.id,'ADGROUP:'||g.id,'AD',g.id,a.id,NULL
 FROM planned_ad a JOIN planned_group g ON g.tenant_id=a.tenant_id AND g.preview_id=a.preview_id AND g.id=a.group_id
 WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit
 UNION ALL SELECT 7,'READBACK:AD:'||a.id,'AD:'||a.id,'READBACK',g.id,a.id,NULL
 FROM planned_ad a JOIN planned_group g ON g.tenant_id=a.tenant_id AND g.preview_id=a.preview_id AND g.id=a.group_id
 WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND g.unit_id=:unit)
SELECT * FROM candidates c WHERE NOT EXISTS (
 SELECT 1 FROM execution_step s WHERE s.tenant_id=:tenant AND s.submission_id=:submission
 AND s.step_key=CAST(:unit AS text)||':'||c.k)
ORDER BY priority,k LIMIT :limit
"""


def expand_submission(
    session: Session, *, context: TenantContext, submission_id: UUID, limit: int = 100
) -> bool:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("expansion limit must be between 1 and 100")
    authorize(session, context, "build")
    row = submission_row(session, context, submission_id, lock=True)
    if row.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    if row.expanded:
        return True
    used = 0
    while used < limit:
        unit = session.exec(
            select(SubmissionUnit)
            .where(
                SubmissionUnit.tenant_id == context.tenant_id,
                SubmissionUnit.submission_id == row.id,
                col(SubmissionUnit.expanded).is_(False),
            )
            .order_by(col(SubmissionUnit.unit_id))
            .limit(1)
        ).first()
        if unit is None:
            after = row.progress.get("after")
            stmt = select(BuildUnit).where(
                BuildUnit.tenant_id == context.tenant_id,
                BuildUnit.preview_id == row.preview_id,
            )
            if after:
                stmt = stmt.where(BuildUnit.id > UUID(after))
            frozen = session.exec(stmt.order_by(col(BuildUnit.id)).limit(1)).first()
            if frozen is None:
                row.expanded = True
                row.progress = {"phase": "done"}
                row.updated_at = datetime.now(UTC)
                session.add(row)
                session.flush()
                return True
            covered = bool(
                SQLAlchemySession.execute(
                    session,
                    text(
                        "SELECT "
                        + COVERED
                        + " FROM build_unit u WHERE u.id=:unit AND u.tenant_id=:tenant"
                    ),
                    dict(params(row), unit=frozen.id),
                ).scalar_one()
            )
            reason = (
                "previous_submission"
                if covered
                else (
                    frozen.reason_codes[0] if frozen.reason_codes else "preview_blocked"
                )
            )
            included = (
                frozen.complete
                and frozen.readiness in {"READY", "PREPARING"}
                and not covered
            )
            unit = SubmissionUnit(
                tenant_id=row.tenant_id,
                submission_id=row.id,
                unit_id=frozen.id,
                preview_id=row.preview_id,
                bc_id=row.bc_id,
                disposition="INCLUDED" if included else "EXCLUDED",
                reason_code=None if included else reason,
                expanded=not included,
            )
            session.add(unit)
            if included:
                session.add(
                    DraftUnitReservation(
                        tenant_id=row.tenant_id,
                        draft_id=row.draft_id,
                        drama_id=frozen.drama_id,
                        advertiser_id=frozen.advertiser_id,
                        bc_id=row.bc_id,
                        submission_id=row.id,
                        preview_id=row.preview_id,
                        unit_id=frozen.id,
                    )
                )
            row.progress = {"phase": "units", "after": str(frozen.id)}
            session.add(row)
            session.flush()
            used += 1
            continue
        frozen = session.exec(
            select(BuildUnit).where(
                BuildUnit.tenant_id == row.tenant_id,
                BuildUnit.id == unit.unit_id,
                BuildUnit.preview_id == row.preview_id,
            )
        ).one()
        candidates = (
            SQLAlchemySession.execute(
                session,
                text(BLUEPRINTS),
                dict(
                    params(row),
                    unit=frozen.id,
                    drama=frozen.drama_id,
                    limit=limit - used,
                ),
            )
            .mappings()
            .all()
        )
        if not candidates:
            unit.expanded = True
            from app.modules.builds.submission_tasks import queue_execution_unit

            queue_execution_unit(session, submission=row, unit=unit)
            session.add(unit)
            session.flush()
            used += 1
            continue
        for candidate in candidates:
            key = f"{frozen.id}:{candidate['k']}"
            parent = (
                uuid5(row.id, f"{frozen.id}:{candidate['parent']}")
                if candidate["parent"]
                else None
            )
            session.add(
                ExecutionStep(
                    id=uuid5(row.id, key),
                    tenant_id=row.tenant_id,
                    submission_id=row.id,
                    preview_id=row.preview_id,
                    bc_id=row.bc_id,
                    unit_id=frozen.id,
                    kind=candidate["kind"],
                    step_key=key,
                    group_id=candidate["group_id"],
                    planned_ad_id=candidate["ad_id"],
                    material_id=candidate["material_id"],
                    parent_step_id=parent,
                )
            )
            # Preserve parent insertion ordering for the self-reference FK.
            session.flush()
            used += 1
    row.updated_at = datetime.now(UTC)
    session.add(row)
    return False


def load_execution_unit(
    session: Session, *, context: TenantContext, submission_id: UUID, unit_id: UUID
) -> ExecutionUnit:
    authorize(session, context)
    row = submission_row(session, context, submission_id)
    unit = session.get(SubmissionUnit, (context.tenant_id, submission_id, unit_id))
    if unit is None or unit.disposition != "INCLUDED":
        raise DomainError("resource_not_found", "执行组合不存在")
    frozen = load_frozen_unit(session, context=context, unit_id=unit_id)
    if frozen.preview_id != row.preview_id:
        raise DomainError("resource_not_found", "执行组合不存在")
    return ExecutionUnit(submission_id=row.id, actor_id=row.actor_id, frozen=frozen)


def claim_step(
    session: Session,
    *,
    context: TenantContext,
    step_id: UUID,
    owner: UUID,
    lease_seconds: int = 60,
) -> StepClaim | None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 960:
        raise ValueError("invalid execution lease")
    authorize(session, context, "build")
    step = session.exec(
        select(ExecutionStep)
        .where(
            ExecutionStep.tenant_id == context.tenant_id, ExecutionStep.id == step_id
        )
        .with_for_update()
    ).one_or_none()
    if step is None:
        raise DomainError("resource_not_found", "执行步骤不存在")
    row = submission_row(session, context, step.submission_id)
    if row.actor_id != context.actor_id:
        raise DomainError("action_forbidden", "提交操作者不匹配")
    now = datetime.now(UTC)
    if (
        step.status in {"SUCCEEDED", "FAILED", "UNKNOWN"}
        or step.remote_id
        or step.phase == "REQUEST_ARMED"
        or step.due_at > now
        or (step.lease_expires_at is not None and step.lease_expires_at > now)
    ):
        return None
    unit_state = session.get(SubmissionUnit, (row.tenant_id, row.id, step.unit_id))
    if unit_state is None or not unit_state.expanded:
        return None
    if step.kind == "CAMPAIGN":
        cta = session.exec(
            select(ExecutionStep.status).where(
                ExecutionStep.tenant_id == row.tenant_id,
                ExecutionStep.submission_id == row.id,
                ExecutionStep.unit_id == step.unit_id,
                ExecutionStep.kind == "CTA",
            )
        ).one_or_none()
        if (
            cta != "SUCCEEDED"
            or group_material_state(
                session, context=context, submission_id=row.id, unit_id=step.unit_id
            )
            != "READY"
        ):
            return None
    if (
        step.kind == "ADGROUP"
        and group_material_state(
            session,
            context=context,
            submission_id=row.id,
            unit_id=step.unit_id,
            group_id=step.group_id,
        )
        != "READY"
    ):
        return None
    if step.parent_step_id:
        parent = session.get(ExecutionStep, step.parent_step_id)
        if parent is None or parent.status != "SUCCEEDED":
            return None
    frozen = session.exec(
        select(BuildUnit).where(
            BuildUnit.tenant_id == row.tenant_id, BuildUnit.id == step.unit_id
        )
    ).one()
    access = resolve_account_access(
        session,
        context=context,
        bc_id=row.bc_id,
        advertiser_id=frozen.advertiser_id,
        action="build",
    )
    if access.connection_id != frozen.connection_id:
        raise DomainError("account_access_denied", "冻结账户授权已变更")
    step.status, step.phase = "RUNNING", "CLAIMED"
    step.attempt += 1
    step.lease_token, step.lease_expires_at = (
        owner,
        now + timedelta(seconds=lease_seconds),
    )
    step.updated_at = now
    session.add(step)
    session.flush()
    return StepClaim(
        step_id=step.id,
        tenant_id=row.tenant_id,
        submission_id=row.id,
        preview_id=row.preview_id,
        unit_id=step.unit_id,
        actor_id=row.actor_id,
        bc_id=row.bc_id,
        advertiser_id=frozen.advertiser_id,
        kind=step.kind,
        group_id=step.group_id,
        planned_ad_id=step.planned_ad_id,
        material_id=step.material_id,
        parent_step_id=step.parent_step_id,
        lease_token=owner,
        lease_expires_at=step.lease_expires_at,
        attempt=step.attempt,
        dispatch_revision=step.dispatch_revision,
    )


def aggregate_status(states: Any) -> str:
    values = set(states)
    if values.intersection({"UNKNOWN", "MISMATCH"}):
        return "NEEDS_REVIEW"
    if values.intersection({"PENDING", "RUNNING", "RETRYABLE"}):
        return "RUNNING"
    if values == {"SUCCEEDED"}:
        return "COMPLETED"
    if "SUCCEEDED" in values and "FAILED" in values:
        return "PARTIAL"
    if values == {"FAILED"}:
        return "FAILED"
    return "QUEUED"


SCOPE_CTE = (
    "WITH scope_basis AS (SELECT u.*, "
    + COVERED
    + """ covered
    FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview),
    scope AS (SELECT b.*, (b.complete AND b.readiness IN ('READY','PREPARING') AND NOT b.covered) included FROM scope_basis b)
    """
)


def get_submission(
    session: Session, *, context: TenantContext, submission_id: UUID
) -> SubmissionView:
    from app.modules.builds.execution_schemas import ObjectCounts, SubmissionView

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    preview = session.get(BuildPreview, row.preview_id)
    assert preview
    scope = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + """SELECT count(*) planned_c,coalesce(sum(group_count),0) planned_g,coalesce(sum(ad_count),0) planned_a,
 count(*) FILTER(WHERE included) submitted_c,coalesce(sum(group_count) FILTER(WHERE included),0) submitted_g,coalesce(sum(ad_count) FILTER(WHERE included),0) submitted_a,
 count(*) FILTER(WHERE NOT included) excluded_units,count(DISTINCT drama_id) dramas,count(DISTINCT advertiser_id) accounts
 FROM scope"""
            ),
            params(row),
        )
        .mappings()
        .one()
    )
    outcome = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + ", material_groups AS ("
                + GROUP_MATERIAL_SQL
                + """),
 objects AS (
 SELECT u.id unit_id,'CAMPAIGN' kind,NULL::uuid group_id,NULL::uuid ad_id FROM scope u WHERE included
 UNION ALL SELECT u.id,'ADGROUP',g.id,NULL FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id WHERE included
 UNION ALL SELECT u.id,'AD',g.id,a.id FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id WHERE included),
 results AS (
 SELECT o.kind, CASE
 WHEN nullif(trim(s.remote_id),'') IS NOT NULL THEN 'succeeded'
 WHEN s.status IN ('SUCCEEDED','UNKNOWN') THEN 'unknown'
 WHEN s.status='FAILED' OR EXISTS (SELECT 1 FROM execution_step ancestor WHERE ancestor.tenant_id=:tenant AND ancestor.submission_id=:submission AND ancestor.unit_id=o.unit_id AND ancestor.status='FAILED' AND (ancestor.kind='CTA' OR (ancestor.kind='CAMPAIGN' AND o.kind!='CAMPAIGN') OR (ancestor.kind='ADGROUP' AND o.kind='AD' AND ancestor.group_id=o.group_id)))
 OR (o.kind='CAMPAIGN' AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.unit_id=o.unit_id) AND NOT EXISTS (SELECT 1 FROM material_groups mg WHERE mg.unit_id=o.unit_id AND NOT mg.failed))
 OR (o.kind IN ('ADGROUP','AD') AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.group_id=o.group_id AND mg.failed)) THEN 'failed'
 ELSE 'pending' END outcome
 FROM objects o LEFT JOIN execution_step s ON s.tenant_id=:tenant AND s.submission_id=:submission AND s.unit_id=o.unit_id AND s.kind=o.kind AND s.group_id IS NOT DISTINCT FROM o.group_id AND s.planned_ad_id IS NOT DISTINCT FROM o.ad_id)
 SELECT kind,outcome,count(*) n FROM results GROUP BY kind,outcome"""
            ),
            params(row),
        )
        .mappings()
        .all()
    )
    counts = {
        key: ObjectCounts() for key in ["succeeded", "failed", "unknown", "pending"]
    }
    fields = {
        "CAMPAIGN": "campaign_count",
        "ADGROUP": "adgroup_count",
        "AD": "ad_count",
    }
    for result in outcome:
        setattr(counts[result["outcome"]], fields[result["kind"]], result["n"])
    stage = (
        SQLAlchemySession.execute(
            session,
            text(
                "SELECT kind,status,count(*) n,bool_or(mismatch) mismatch FROM execution_step WHERE tenant_id=:tenant AND submission_id=:submission GROUP BY kind,status"
            ),
            params(row),
        )
        .mappings()
        .all()
    )
    stage_counts = {f"{r['kind']}:{r['status']}": r["n"] for r in stage}
    states = {r["status"] for r in stage}
    if any(r["mismatch"] for r in stage):
        states.add("MISMATCH")
    if not row.expanded:
        states.add("PENDING" if stage else "QUEUED")
    if any(getattr(counts["unknown"], name) for name in fields.values()):
        states.add("UNKNOWN")
    status = aggregate_status(states)
    failed_objects = sum(getattr(counts["failed"], field) for field in fields.values())
    pending_objects = sum(
        getattr(counts["pending"], field) for field in fields.values()
    )
    succeeded_objects = sum(
        getattr(counts["succeeded"], field) for field in fields.values()
    )
    if status != "NEEDS_REVIEW" and failed_objects and not pending_objects:
        # Descendants that can never run do not keep an all-failed task running
        # while the executor records their explicit dependency_failed facts.
        status = "PARTIAL" if succeeded_objects else "FAILED"
    if not stage and not row.expanded:
        status = "QUEUED"
    if row.expanded and scope["submitted_c"] == 0:
        status = "COMPLETED"
    planned = ObjectCounts(
        campaign_count=scope["planned_c"],
        adgroup_count=scope["planned_g"],
        ad_count=scope["planned_a"],
    )
    submitted = ObjectCounts(
        campaign_count=scope["submitted_c"],
        adgroup_count=scope["submitted_g"],
        ad_count=scope["submitted_a"],
    )
    excluded = ObjectCounts(
        **{
            name: getattr(planned, name) - getattr(submitted, name)
            for name in fields.values()
        }
    )
    return SubmissionView(
        submission_id=row.id,
        preview_id=row.preview_id,
        draft_id=row.draft_id,
        batch_short_id=preview.batch_short_id,
        bc_id=row.bc_id,
        status=status,
        expanded=row.expanded,
        currency=preview.config["currency"],
        daily_budget_sum=str(preview.budget * scope["submitted_c"]),
        planned=planned,
        submitted=submitted,
        excluded=excluded,
        stage_counts=stage_counts,
        excluded_unit_count=scope["excluded_units"],
        drama_count=scope["dramas"],
        account_count=scope["accounts"],
        created_at=row.created_at,
        updated_at=row.updated_at,
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        unknown=counts["unknown"],
        pending=counts["pending"],
    )


def _page_scope(
    context: TenantContext,
    row: Submission,
    kind: str,
    limit: int,
    cursor: str | None,
    filters: dict[str, str | None],
) -> tuple[dict[str, str | None], UUID | None]:
    from app.modules.accounts.resolver import decode_cursor

    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("invalid_page_size", "分页范围为1到100")
    scope = {
        "tenant": str(context.tenant_id),
        "bc": row.bc_id,
        "submission": str(row.id),
        "kind": kind,
        "limit": str(limit),
        **filters,
    }
    after = decode_cursor(cursor, scope=scope)
    try:
        identity = UUID(after) if after else None
    except ValueError, TypeError, AttributeError:
        raise DomainError("invalid_cursor", "分页游标无效") from None
    return scope, identity


def get_submission_units(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
    excluded_only: bool = False,
) -> Page[SubmissionUnitPublic]:
    from app.core.pagination import Page
    from app.modules.accounts.resolver import encode_cursor
    from app.modules.builds.execution_schemas import SubmissionUnitPublic

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    scope, after = _page_scope(
        context,
        row,
        "units",
        limit,
        cursor,
        {
            "advertiser": advertiser_id,
            "drama": str(drama_id) if drama_id else None,
            "excluded": str(excluded_only),
        },
    )
    results = (
        SQLAlchemySession.execute(
            session,
            text(
                SCOPE_CTE
                + """SELECT u.id,u.drama_id,d.title,u.advertiser_id,u.campaign_name,u.included,u.covered,u.reason_codes,
 coalesce(su.expanded,false) expanded
 FROM scope u JOIN preview_drama d ON d.tenant_id=u.tenant_id AND d.preview_id=u.preview_id AND d.drama_id=u.drama_id
 LEFT JOIN submission_unit su ON su.tenant_id=u.tenant_id AND su.submission_id=:submission AND su.unit_id=u.id
 WHERE (CAST(:after AS uuid) IS NULL OR u.id>CAST(:after AS uuid))
 AND (CAST(:advertiser AS text) IS NULL OR u.advertiser_id=:advertiser)
 AND (CAST(:drama AS uuid) IS NULL OR u.drama_id=CAST(:drama AS uuid))
 AND (NOT :excluded OR NOT u.included) ORDER BY u.id LIMIT :limit"""
            ),
            dict(
                params(row),
                after=str(after) if after else None,
                advertiser=advertiser_id,
                drama=str(drama_id) if drama_id else None,
                excluded=excluded_only,
                limit=limit + 1,
            ),
        )
        .mappings()
        .all()
    )
    items = [
        SubmissionUnitPublic(
            unit_id=r["id"],
            drama_id=r["drama_id"],
            title=r["title"],
            advertiser_id=r["advertiser_id"],
            campaign_name=r["campaign_name"],
            disposition="INCLUDED" if r["included"] else "EXCLUDED",
            reason_codes=[]
            if r["included"]
            else (
                ["previous_submission"]
                if r["covered"]
                else (r["reason_codes"] or ["preview_blocked"])
            ),
            expanded=r["expanded"],
        )
        for r in results[:limit]
    ]
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].unit_id))
        if len(results) > limit
        else None,
    )


def get_submission_steps(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
    advertiser_id: str | None = None,
    drama_id: UUID | None = None,
    kind: str | None = None,
    result: str | None = None,
) -> Page[StepPublic]:
    from app.core.pagination import Page
    from app.modules.accounts.resolver import encode_cursor
    from app.modules.builds.execution_schemas import StepPublic

    authorize(session, context)
    row = submission_row(session, context, submission_id)
    if kind is not None and kind not in {
        "MATERIAL",
        "CTA",
        "CAMPAIGN",
        "ADGROUP",
        "AD",
        "READBACK",
    }:
        raise DomainError("invalid_resolve_request", "步骤种类无效")
    if result is not None and result not in {
        "QUEUED",
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "RETRYABLE",
        "UNKNOWN",
    }:
        raise DomainError("invalid_resolve_request", "步骤结果无效")
    scope, after = _page_scope(
        context,
        row,
        "steps",
        limit,
        cursor,
        {
            "advertiser": advertiser_id,
            "drama": str(drama_id) if drama_id else None,
            "stepkind": kind,
            "result": result,
        },
    )
    stmt = (
        select(ExecutionStep)
        .join(
            BuildUnit,
            (col(BuildUnit.id) == ExecutionStep.unit_id)
            & (col(BuildUnit.tenant_id) == ExecutionStep.tenant_id),
        )
        .where(
            ExecutionStep.tenant_id == row.tenant_id,
            ExecutionStep.submission_id == row.id,
        )
    )
    if after:
        stmt = stmt.where(ExecutionStep.id > after)
    if advertiser_id:
        stmt = stmt.where(BuildUnit.advertiser_id == advertiser_id)
    if drama_id:
        stmt = stmt.where(BuildUnit.drama_id == drama_id)
    if kind:
        stmt = stmt.where(ExecutionStep.kind == kind)
    if result:
        stmt = stmt.where(ExecutionStep.status == result)
    rows = session.exec(stmt.order_by(col(ExecutionStep.id)).limit(limit + 1)).all()
    items = [
        StepPublic(
            step_id=s.id,
            unit_id=s.unit_id,
            kind=s.kind,
            group_id=s.group_id,
            planned_ad_id=s.planned_ad_id,
            material_id=s.material_id,
            status=s.status,
            remote_id=s.remote_id,
            error_code=s.error_code,
            operation_status=s.operation_status,
            review_status=s.review_status,
            mismatch=s.mismatch,
            checked_at=s.checked_at,
        )
        for s in rows[:limit]
    ]
    return Page(
        items=items,
        next_cursor=encode_cursor(scope=scope, last_id=str(items[-1].step_id))
        if len(rows) > limit
        else None,
    )


GROUP_MATERIAL_SQL = """
SELECT g.id group_id,g.unit_id,
 count(m.material_id)>0 AND coalesce(bool_and(ms.status='SUCCEEDED'),false) AND count(m.material_id)=count(ms.id) ready,
 coalesce(bool_or(ms.status='FAILED'),false) failed
FROM planned_group g
LEFT JOIN preview_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
LEFT JOIN execution_step ms ON ms.tenant_id=g.tenant_id AND ms.submission_id=:submission AND ms.unit_id=g.unit_id AND ms.kind='MATERIAL' AND ms.material_id=m.material_id
WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND (CAST(:unit AS uuid) IS NULL OR g.unit_id=CAST(:unit AS uuid))
GROUP BY g.id,g.unit_id
"""


def group_material_state(
    session: Session,
    *,
    context: TenantContext,
    submission_id: UUID,
    unit_id: UUID,
    group_id: UUID | None = None,
) -> str:
    """One group: all materials ready; whole unit: any complete group ready."""
    authorize(session, context)
    row = submission_row(session, context, submission_id)
    value = (
        SQLAlchemySession.execute(
            session,
            text(
                "WITH material_groups AS ("
                + GROUP_MATERIAL_SQL
                + """
    ) SELECT coalesce(bool_or(ready),false) ready,coalesce(bool_and(failed),false) failed
    FROM material_groups WHERE CAST(:group AS uuid) IS NULL OR group_id=CAST(:group AS uuid)
    """
            ),
            dict(params(row), unit=unit_id, group=group_id),
        )
        .mappings()
        .one()
    )
    return "READY" if value["ready"] else "FAILED" if value["failed"] else "PENDING"
