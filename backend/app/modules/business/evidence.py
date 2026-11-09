"""赔付依据读取与记录；空依据与已核实无赔付分开。"""

from datetime import date
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session

from app.core.context import TenantContext
from app.modules.business.configuration import scope_accounts
from app.modules.business.models import BusinessEvidence
from app.modules.operation_logs.models import OperationDayEvidence


def read_evidence(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    advertiser_id: str,
    campaign_id: str,
    stat_date: date,
) -> BusinessEvidence | None:
    if advertiser_id not in scope_accounts(session, context, bc_id):
        raise HTTPException(403, "当前BC未授权账户")
    return session.get(
        BusinessEvidence,
        (context.tenant_id, bc_id, advertiser_id, campaign_id, stat_date),
    )


def latest_auto(
    session: Session,
    *,
    tenant_id: UUID,
    advertiser_id: str,
    campaign_id: str,
    stat_date: date,
    bc_id: str | None = None,
) -> OperationDayEvidence | None:
    from sqlmodel import col, select

    from app.modules.operation_logs.models import OperationDayEvidence

    if bc_id is not None:
        from app.modules.business.calculation import quarter_start
        from app.modules.business.models import BusinessDay, BusinessScope

        scope = session.get(BusinessScope, (tenant_id, bc_id))
        run_id = (
            scope.published_runs.get(quarter_start(stat_date).isoformat())
            if scope
            else None
        )
        if run_id:
            source = session.exec(
                select(col(BusinessDay.compensation_source)).where(
                    BusinessDay.tenant_id == tenant_id,
                    BusinessDay.bc_id == bc_id,
                    BusinessDay.run_id == UUID(run_id),
                    BusinessDay.advertiser_id == advertiser_id,
                    BusinessDay.campaign_id == campaign_id,
                    BusinessDay.stat_date == stat_date,
                )
            ).one_or_none()
            if source and source.startswith(
                ("official-operation-evidence:", "admin-correction:")
            ):
                evidence_id = UUID(source.split(":")[1])
                evidence = session.get(OperationDayEvidence, evidence_id)
                if (
                    evidence is not None
                    and evidence.tenant_id == tenant_id
                    and evidence.advertiser_id == advertiser_id
                    and evidence.campaign_id == campaign_id
                    and evidence.stat_date == stat_date
                ):
                    return evidence
    return session.exec(
        select(OperationDayEvidence)
        .where(
            OperationDayEvidence.tenant_id == tenant_id,
            OperationDayEvidence.advertiser_id == advertiser_id,
            OperationDayEvidence.campaign_id == campaign_id,
            OperationDayEvidence.stat_date == stat_date,
        )
        .order_by(col(OperationDayEvidence.created_at).desc())
    ).first()


def auto_is_current(session: Session, auto: OperationDayEvidence) -> bool:
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.operation_logs.evidence import frozen_sources
    from app.modules.reporting.store_models import ReportSyncState

    current = frozen_sources(
        session,
        auto.tenant_id,
        [auto.advertiser_id],
        date.fromisoformat(auto.data["source_start"]),
        date.fromisoformat(auto.data["source_end"]),
    )
    report = session.get(
        ReportSyncState, (auto.tenant_id, auto.advertiser_id, "ad_day", auto.stat_date)
    )
    account = session.get(AdvertiserAccount, (auto.tenant_id, auto.advertiser_id))
    return (
        auto.data.get("sources")
        == current["account_signatures"].get(auto.advertiser_id)
        and auto.data.get("report_revision")
        == (report.publication_version if report else None)
        and account is not None
        and account.timezone == auto.data.get("timezone")
    )
