"""真实 PG 区分排队意图时间与实际完整读取的首页观测时间。"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import jobs, publish
from app.modules.reporting.store_models import (
    AccountReconciliationDay,
    AdReportDay,
    CampaignReportHour,
    CreativeReportDay,
    ReportSyncJob,
    ReportSyncState,
)
from tests.modules.reporting.creative_job_helpers import (
    ACCOUNTS,
    DAY,
    claim,
    row,
    run,
)
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import _route

MODELS = {
    "ad_day": AdReportDay,
    "creative_day": CreativeReportDay,
    "campaign_hour": CampaignReportHour,
    "account_day": AccountReconciliationDay,
}


def report_row(kind, spend):
    if kind == "ad_day":
        return ad_row("a1", DAY, spend=spend)
    if kind == "creative_day":
        return row("1", spend=spend)
    if kind == "campaign_hour":
        return {
            "dimensions": {"stat_time_hour": f"{DAY} 10:00:00", "campaign_id": "c1"},
            "metrics": {"spend": spend},
        }
    return {"dimensions": {"stat_time_day": str(DAY)}, "metrics": {"spend": spend}}


def publish_previous(factory, seed, kind="creative_day", *, day=DAY):
    with factory() as session:
        kwargs = {"tenant_id": seed.tenant_id, "accounts": ACCOUNTS}
        if kind == "campaign_hour":
            kwargs |= {"job_id": uuid4(), "day": day}
        else:
            kwargs |= {"start": day, "end": day}
            if kind != "account_day":
                kwargs["job_id"] = uuid4()
        publisher = getattr(
            publish,
            "publish_account_reconciliation"
            if kind == "account_day"
            else f"publish_{kind}",
        )
        previous = report_row(kind, "20")
        if kind != "campaign_hour":
            previous["dimensions"]["stat_time_day"] = str(day)
        publisher(session, **kwargs, rows=[previous])
        stamp = session.get(
            ReportSyncState, (seed.tenant_id, "acc-1", kind, day)
        ).last_success_at
        session.commit()
        return stamp


class FirstRead:
    def __init__(self, factory, rows, error=None):
        self.factory, self.rows, self.error = factory, rows, error
        self.observed = []

    def __getattr__(self, _kind):
        def read(**_kwargs):
            with self.factory() as session:
                self.observed.append(
                    session.connection()
                    .execute(text("SELECT clock_timestamp()"))
                    .scalar_one()
                )
            if self.error:
                raise self.error
            return RawReportPage(
                tuple(self.rows), 1, 1 if self.rows else 0, len(self.rows)
            )

        return read


@pytest.mark.parametrize("kind", MODELS)
@pytest.mark.parametrize("empty", [False, True])
def test_old_queued_job_first_read_accepts_new_lower_or_empty_result(
    committed_sync_seed, kind, empty
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job_id = jobs.enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind=kind,
            tier="hot",
            advertiser_ids=["acc-1"],
            start=DAY,
            end=DAY,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
    stamp = publish_previous(factory, seed, kind)
    job = claim(factory, job_id)
    assert jobs.request_time(job) < stamp
    reader = FirstRead(factory, [] if empty else [report_row(kind, "2")])
    assert run(factory, job, reader) == "COMPLETE"
    with factory() as session:
        state = session.get(ReportSyncState, (seed.tenant_id, "acc-1", kind, DAY))
        assert state.last_success_at > stamp
        assert state.last_success_at >= reader.observed[0]
        assert state.row_count == (0 if empty else 1)
        if kind == "creative_day":
            observed = datetime.fromisoformat(
                session.get(ReportSyncJob, job.id).progress["snapshot_observed_at"]
            )
            assert stamp < observed <= reader.observed[0]
        records = session.exec(
            select(MODELS[kind]).where(MODELS[kind].tenant_id == seed.tenant_id)
        ).all()
        if kind == "account_day":
            assert len(records) == 1
            assert records[0].account_spend == (None if empty else Decimal("2"))
        else:
            assert len(records) == (0 if empty else 1)
            if records:
                assert records[0].spend == Decimal("2")


@pytest.mark.parametrize(
    "kind", ["ad_day", "campaign_hour", "account_day", "creative_day"]
)
def test_full_read_retry_takes_new_observation_after_later_publish(
    committed_sync_seed, kind
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job_id = jobs.enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind=kind,
            tier="hot",
            advertiser_ids=["acc-1"],
            start=DAY,
            end=DAY,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
        )
        session.commit()
    reader = FirstRead(
        factory, [report_row(kind, "2")], DomainError("tiktok_response_error", "retry")
    )
    assert run(factory, claim(factory, job_id), reader) == "WAITING"
    stamp = publish_previous(factory, seed, kind)
    reader.error = None
    assert run(factory, claim(factory, job_id), reader) == "COMPLETE"
    with factory() as session:
        assert (
            session.get(
                ReportSyncState, (seed.tenant_id, "acc-1", kind, DAY)
            ).last_success_at
            > stamp
        )
