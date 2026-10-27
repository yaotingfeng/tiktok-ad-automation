"""历史素材在有界时间内连续读完整页，减少大积压中的整池续步等待。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting import jobs
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_material_resume import (
    DAY,
    claim,
    enqueue,
    facts,
    pages,
    row,
    run,
)


class MaterialTransport:
    """只替代SDK传输边界；真实读取器、分页校验、事务和发布器继续运行。"""

    def __init__(self, *, count, day=DAY, page_seconds=0, denied_page=None):
        self.count, self.day = count, day
        self.page_seconds, self.denied_page = page_seconds, denied_page
        self.elapsed = 0.0
        self.request_count = 0
        self.sent = []
        self.changed_page = None

    def __call__(self, operation, advertiser, arguments):
        assert operation == "reports.material_breakdown"
        assert advertiser == "acc-1"
        assert arguments["start_date"] == arguments["end_date"] == str(self.day)
        assert arguments["page_size"] == 100
        page = arguments["page"]
        if page == self.denied_page:
            raise AccountAdmissionDeferred(1000, source="local", reason="endpoint_rate")
        self.request_count += 1
        self.sent.append(page)
        self.elapsed += self.page_seconds
        count = self.count + int(page == self.changed_page)
        return {
            "list": [row(page, day=self.day)],
            "page_info": {
                "page": page,
                "page_size": 1,
                "total_page": count,
                "total_number": count,
            },
        }


def test_backfill_reads_fifty_pages_then_yields_without_partial_publish(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    transport = MaterialTransport(count=51)
    reader = SdkReportReader(send=transport)
    assert run(factory, job, reader) == "WAITING"
    assert transport.sent == list(range(1, 51))
    assert len(pages(factory, job.id)) == 50
    assert facts(factory, seed.tenant_id) == []
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        observed = saved.progress["material_paging"]["observed_at"]
        assert saved.request_count == 50
        assert saved.claim_token is saved.claimed_until is None
        assert saved.due_at > job.due_at
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert transport.sent == list(range(1, 52))
    assert len(facts(factory, seed.tenant_id)) == 51
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        assert saved.request_count == 51
        assert saved.progress["material_paging"]["observed_at"] == observed


@pytest.mark.parametrize("page_seconds,want_pages", [(5, 6), (31, 1)])
def test_backfill_stops_at_complete_page_after_thirty_seconds(
    committed_sync_seed, monkeypatch, page_seconds, want_pages
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    transport = MaterialTransport(count=12, page_seconds=page_seconds)
    monkeypatch.setattr(jobs, "monotonic", lambda: transport.elapsed, raising=False)
    reader = SdkReportReader(send=transport)
    assert run(factory, job, reader) == "WAITING"
    assert transport.sent == list(range(1, want_pages + 1))
    assert len(pages(factory, job.id)) == want_pages
    assert not facts(factory, seed.tenant_id)
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        assert saved.progress["material_paging"]["next_page"] == want_pages + 1
        assert saved.error_code is None and saved.attempts == 0
    transport.page_seconds = 0
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert transport.sent == list(range(1, 13))


def test_backfill_admission_defers_immediately_and_preserves_physical_call_count(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    transport = MaterialTransport(count=12, denied_page=8)
    reader = SdkReportReader(send=transport)
    assert run(factory, job, reader) == "WAITING"
    assert transport.sent == list(range(1, 8))
    assert len(pages(factory, job.id)) == 7
    assert not facts(factory, seed.tenant_id)
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        assert saved.error_code == "admission_deferred"
        assert saved.request_count == 7 and saved.attempts == 0
        assert saved.due_at > datetime.now(UTC)
        assert saved.progress["material_paging"]["next_page"] == 8
        assert saved.claim_token is saved.claimed_until is None
    transport.denied_page = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert transport.sent == list(range(1, 13))


@pytest.mark.parametrize("closeout", [False, True])
def test_local_today_and_yesterday_closeout_keep_five_page_steps(
    committed_sync_seed, monkeypatch, closeout
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 香港已经28日，UTC仍27日；分类必须沿账户当地日，不能只看UTC。
            return cls(2026, 10, 27, 18, tzinfo=UTC)

    monkeypatch.setattr(jobs, "datetime", Clock)
    seed, factory = committed_sync_seed
    day = datetime(2026, 10, 28).date() - timedelta(days=int(closeout))
    job = enqueue(factory, seed, start=day, end=day)
    if closeout:
        with factory() as session:
            session.get(ReportSyncJob, job.id).priority = 0
            session.commit()
    transport = MaterialTransport(count=7, day=day)
    assert run(factory, job, SdkReportReader(send=transport)) == "WAITING"
    assert transport.sent == [1, 2, 3, 4, 5]
    assert not facts(factory, seed.tenant_id)


def test_backfill_extended_step_still_rejects_changed_snapshot(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = enqueue(factory, seed)
    transport = MaterialTransport(count=12)
    transport.changed_page = 8
    reader = SdkReportReader(send=transport)
    assert run(factory, job, reader) == "WAITING"
    with factory() as session:
        saved = session.get(ReportSyncJob, job.id)
        assert saved.error_code == "report_snapshot_changed"
        assert saved.attempts == 1 and saved.request_count == 8
        assert "material_paging" not in saved.progress
    assert not pages(factory, job.id)
    assert not facts(factory, seed.tenant_id)
    transport.changed_page = None
    assert run(factory, claim(factory, job.id), reader) == "COMPLETE"
    assert transport.sent == list(range(1, 9)) + list(range(1, 13))
    assert len(facts(factory, seed.tenant_id)) == 12
