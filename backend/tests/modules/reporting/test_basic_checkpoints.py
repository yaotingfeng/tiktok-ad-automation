"""多页财务报表受限后从已验证页继续，公开事实始终完整发布。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting.jobs import claim_jobs, enqueue_job, execute_job
from app.modules.reporting.store_models import (
    AdReportDay,
    ReportSyncJob,
    ReportSyncPage,
)
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import D, _route


def test_admission_after_first_page_resumes_second_without_repeating_remote_read(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
        )
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0]
        token = job.claim_token
        session.commit()

    class Reader:
        calls = []
        defer = True

        def ad_day(self, *, page, **_kwargs):
            self.calls.append(page)
            if page == 2 and self.defer:
                raise AccountAdmissionDeferred(1000)
            return RawReportPage((ad_row(f"a{page}", D),), page, 2, 2)

    reader = Reader()
    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
        == "WAITING"
    )
    with factory() as session:
        assert not session.exec(select(AdReportDay)).all()
        assert (
            len(
                session.exec(
                    select(ReportSyncPage).where(ReportSyncPage.job_id == job_id)
                ).all()
            )
            == 1
        )
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=2), limit=1
        )[0]
        token = job.claim_token
        observed = job.progress["basic_paging"]["observed_at"]
        session.commit()
    reader.defer = False
    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
        == "COMPLETE"
    )
    assert reader.calls == [1, 2, 2]
    with factory() as session:
        assert {r.ad_id for r in session.exec(select(AdReportDay)).all()} == {
            "a1",
            "a2",
        }
        assert (
            session.get(ReportSyncJob, job_id).progress["basic_snapshot"]["observed_at"]
            == observed
        )


@pytest.mark.parametrize("change", ["snapshot", "scope"])
def test_changed_snapshot_restarts_and_changed_scope_never_reuses_pages(
    committed_sync_seed, change
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
        )
        token = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0].claim_token
        session.commit()

    class Reader:
        total = 2
        defer = True
        calls = []

        def ad_day(self, *, page, **_kwargs):
            self.calls.append(page)
            if page == 2 and self.defer:
                raise AccountAdmissionDeferred(1000)
            return RawReportPage((ad_row(f"a{page}", D),), page, self.total, self.total)

    reader = Reader()
    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
        == "WAITING"
    )
    with factory() as session:
        job = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=2), limit=1
        )[0]
        token = job.claim_token
        if change == "scope":
            job.campaign_ids = ["changed"]
        session.commit()
    reader.defer = False
    reader.total = 3
    result = execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
    assert result == ("FAILED" if change == "scope" else "WAITING")
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert not session.exec(select(AdReportDay)).all()
        assert job.error_code == (
            "report_query_invalid" if change == "scope" else "report_snapshot_changed"
        )
        if change == "snapshot":
            assert "basic_paging" not in job.progress
            assert not session.exec(select(ReportSyncPage)).all()
        else:
            assert reader.calls == [1, 2]


def test_campaign_split_resumes_without_repeating_discovery_or_prior_batch(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job_id = enqueue_job(
            session,
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            actor_id=seed.actor_id,
            frozen_route=_route(seed),
            kind="ad_day",
            tier="hot",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
        )
        token = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=1), limit=1
        )[0].claim_token
        session.commit()

    class Reader:
        calls = []
        defer = True

        def campaign_day(self, **_kwargs):
            self.calls.append("campaigns")
            return RawReportPage(
                tuple({"dimensions": {"campaign_id": f"c{i:02}"}} for i in range(31)),
                1,
                1,
                31,
            )

        def ad_day(self, *, campaign_ids=(), **_kwargs):
            self.calls.append(tuple(campaign_ids))
            if not campaign_ids:
                return RawReportPage((), 1, 20, 20000)
            if len(campaign_ids) == 1 and self.defer:
                raise AccountAdmissionDeferred(1000)
            return RawReportPage(
                tuple(ad_row(c, D, campaign=c) for c in campaign_ids),
                1,
                1,
                len(campaign_ids),
            )

    reader = Reader()
    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
        == "WAITING"
    )
    before = len(reader.calls)
    with factory() as session:
        assert not session.exec(select(AdReportDay)).all()
        token = claim_jobs(
            session, now=datetime.now(UTC) + timedelta(seconds=2), limit=1
        )[0].claim_token
        session.commit()
    reader.defer = False
    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=reader)
        == "COMPLETE"
    )
    assert reader.calls[before:] == [("c30",)]
    with factory() as session:
        assert len(session.exec(select(AdReportDay)).all()) == 31
        assert not session.exec(select(ReportSyncPage)).all()
