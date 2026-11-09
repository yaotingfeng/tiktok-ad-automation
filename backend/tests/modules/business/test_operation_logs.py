from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlmodel import select

from app.modules.business.models import BusinessDay
from app.modules.business.projection import advance_run, request_rebuild
from app.modules.reporting.store_models import CampaignReportDay, ReportSyncState
from tests.modules.operation_logs.test_daily_evidence import anchors
from tests.modules.operation_logs.test_parser import export
from tests.modules.operation_logs.test_store import publish


def test_history_and_t_plus_one_use_same_saved_log_evidence(session, business_case):
    case = business_case
    # 合成官方数据：历史日和T+1均从已保存日报取数，绝不重新请求广告报表。
    for day in (date(2026, 11, 1), date(2026, 11, 2)):
        session.add(
            ReportSyncState(
                tenant_id=case.context.tenant_id,
                advertiser_id=case.advertiser_id,
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                publication_version=1,
                observed_at=datetime(2026, 11, 3, tzinfo=UTC),
            )
        )
        session.add(
            CampaignReportDay(
                tenant_id=case.context.tenant_id,
                advertiser_id=case.advertiser_id,
                campaign_id="campaign-test",
                campaign_name="测试剧",
                stat_date=day,
                currency="USD",
                timezone="UTC",
                spend=Decimal(100),
                revenue_total=Decimal(60),
                revenue_d0=Decimal(60),
                ad_impression_events=Decimal(300),
                fetched_at=datetime(2026, 11, 3, tzinfo=UTC),
                sync_job_id=uuid4(),
            )
        )
    session.flush()
    publish(
        session,
        case,
        export(),
        datetime(2026, 11, 3, tzinfo=UTC),
        end=date(2026, 11, 2),
    )
    anchors(session, case)
    runs = request_rebuild(
        session,
        context=case.context,
        bc_id=case.bc_id,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        now=datetime(2026, 11, 3, 12, tzinfo=UTC),
    )
    for run in runs:
        for _ in range(100):
            if advance_run(session, run.id, now=datetime(2026, 11, 3, 12, tzinfo=UTC)):
                break
        assert run.status == "COMPLETE"
    rows = session.exec(
        select(BusinessDay).where(BusinessDay.run_id == runs[0].id)
    ).all()
    assert len(rows) == 2
    assert all(r.compensation == 50 for r in rows)
    assert all("COMPENSATION_QUALIFICATION_PENDING" in r.issues for r in rows)
    assert all(
        r.compensation_source.startswith("official-operation-evidence:") for r in rows
    )
    # 同账户在别的范围生成较新的日依据，不能替换当前BC已发布行的更正基线。
    from app.modules.business.evidence import latest_auto
    from app.modules.operation_logs.models import OperationDayEvidence

    first = rows[0]
    original = latest_auto(
        session,
        tenant_id=case.context.tenant_id,
        advertiser_id=case.advertiser_id,
        campaign_id=first.campaign_id,
        stat_date=first.stat_date,
        bc_id=case.bc_id,
    )
    foreign = OperationDayEvidence(
        tenant_id=case.context.tenant_id,
        advertiser_id=case.advertiser_id,
        campaign_id=first.campaign_id,
        stat_date=first.stat_date,
        digest="f" * 64,
        data={"scope": "other-range"},
        created_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    session.add(foreign)
    session.flush()
    assert (
        latest_auto(
            session,
            tenant_id=case.context.tenant_id,
            advertiser_id=case.advertiser_id,
            campaign_id=first.campaign_id,
            stat_date=first.stat_date,
            bc_id=case.bc_id,
        ).id
        == original.id
    )


def test_admin_correction_reuses_full_multiday_source_window(
    session, business_case, business_client
):
    from app.modules.ads.models import AdObject
    from app.modules.operation_logs.evidence import build_evidence, frozen_sources

    case = business_case
    publish(
        session,
        case,
        export(),
        datetime(2026, 11, 3, tzinfo=UTC),
        end=date(2026, 11, 2),
    )
    anchors(session, case)
    for obj in session.exec(
        select(AdObject).where(AdObject.tenant_id == case.context.tenant_id)
    ).all():
        obj.observed_at = datetime(2026, 11, 3, tzinfo=UTC)
        obj.published_version += 1
        session.add(obj)
    session.flush()
    frozen = frozen_sources(
        session,
        case.context.tenant_id,
        [case.advertiser_id],
        date(2026, 11, 1),
        date(2026, 11, 2),
    )
    auto = build_evidence(
        session,
        tenant_id=case.context.tenant_id,
        advertiser_id=case.advertiser_id,
        campaign_id="campaign-test",
        day=date(2026, 11, 1),
        timezone="UTC",
        frozen=frozen,
        spend=Decimal(100),
        revenue=Decimal(60),
        events=Decimal(300),
    )
    body = {
        "bc_id": case.bc_id,
        "advertiser_id": case.advertiser_id,
        "campaign_id": "campaign-test",
        "stat_date": "2026-11-01",
        "bid": "1.2",
        "eligible": True,
        "source": "本地合成管理员更正",
        "evidence_revision": auto.digest,
    }
    url = f"/api/tenants/{case.context.tenant_id}/business/evidence"
    response = business_client.put(url, json=body)
    assert response.status_code == 200, response.text
    # 真实源修订后同一更正必须冲突，而不是永久覆盖。
    publish(
        session,
        case,
        export(
            [
                {
                    "action": "Change",
                    "name": "Campaign",
                    "before_after": [{"before": "Enabled", "after": "Paused"}],
                }
            ]
        ),
        datetime(2026, 11, 3, 1, tzinfo=UTC),
        end=date(2026, 11, 2),
    )
    assert business_client.put(url, json=body).status_code == 409
