"""长队列保留分页续点；负证据期限从观察起点计算，不能随完成时刻延长。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.reporting.link_queries import QueryMembershipReader
from app.modules.reporting.material_links import (
    NEGATIVE_SCAN_TTL,
    _scan_scope,
    refresh_material_links,
    wait_for_dependencies,
)
from app.modules.reporting.store_models import MaterialLinkScan, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_link_query_reuse import page
from tests.modules.reporting.test_material_links import D, overview
from tests.modules.reporting.test_material_query import material_row, publish

START = datetime(2026, 10, 5, 23, 55, tzinfo=UTC)


@pytest.mark.parametrize("day", [D - timedelta(days=10), D])
def test_partial_page_resumes_after_queue_delay_without_renewing_cache(day):
    cache, calls = {}, []

    def read(_ads, _first, _last, number):
        calls.append(number)
        return page([f"post-{number}"], number, 2, 2)

    QueryMembershipReader(read, cache, targets=["missing"], now=START)(
        ("ad",), day, day, 1
    )
    resumed = QueryMembershipReader(
        read, cache, targets=["missing"], now=START + timedelta(minutes=15)
    )
    assert resumed(("ad",), day, day, 2).page == 2
    assert calls == [1, 2]
    # 即使同一个 reader 再请求首页，也不能把刚完成的旧集合当成新的负缓存。
    resumed(("ad",), day, day, 1)
    assert calls == [1, 2, 1]


def test_expired_completed_cache_cannot_hit_after_reader_construction():
    cache, calls = {}, []

    def read(*args):
        calls.append(args)
        return page([])

    reader = QueryMembershipReader(read, cache, targets=["missing"], now=START)
    reader(("ad",), D, D, 1)
    # 模拟长分页结束后同一 reader 新增了以原起点计时的完成条目。
    next(iter(cache["entries"].values()))["at"] -= 901
    reader(("ad",), D, D, 1)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change", ["signature", "duplicate", "scope", "page", "targets"]
)
def test_delayed_partial_pages_still_reject_changed_evidence(change):
    cache = {}

    def read(_ads, _first, _last, number):
        return page(
            ["p1" if number == 1 or change == "duplicate" else "p2"],
            number,
            2,
            3 if number > 1 and change == "signature" else 2,
        )

    QueryMembershipReader(read, cache, targets=["missing"], now=START)(("ad",), D, D, 1)
    targets = ["missing", "new"] if change == "targets" else ["missing"]
    resumed = QueryMembershipReader(
        read, cache, targets=targets, now=START + timedelta(minutes=15)
    )
    with pytest.raises(DomainError) as caught:
        resumed(
            ("other" if change == "scope" else "ad",),
            D,
            D,
            3 if change == "page" else 2,
        )
    assert caught.value.code == "report_snapshot_changed"
    assert not cache.get("entries")


def start_scan(session, seed):
    publish(session, seed, "acc-1", [material_row("missing", "5")])
    progress = {}
    args = {
        "tenant_id": seed.tenant_id,
        "advertiser_id": "acc-1",
        "start": D,
        "end": D,
        "read_overview": lambda _page: overview(),
        "read_ad_posts": lambda *_args: overview(),
        "progress": progress,
    }
    assert not refresh_material_links(session, now=START, **args)
    return progress, args


def test_new_negative_scan_records_its_original_observation_time(session, sync_seed):
    progress, _ = start_scan(session, sync_seed)
    assert datetime.fromisoformat(progress["scan_evidence_at"]) == START


@pytest.mark.parametrize("age", [timedelta(minutes=15), timedelta(hours=7)])
def test_completed_negative_evidence_keeps_start_time_and_does_not_extend_backoff(
    session, sync_seed, monkeypatch, age
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    progress, args = start_scan(session, sync_seed)
    now = START + age
    assert refresh_material_links(session, now=now, **args)
    scan = session.get(
        MaterialLinkScan, (sync_seed.tenant_id, "acc-1", _scan_scope(D, D, ()))
    )
    assert scan.scanned_at == START
    assert (scan.evidence_signature is not None) == (age < NEGATIVE_SCAN_TTL)
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    job.progress = dict(progress)
    assert wait_for_dependencies(session, job, now=now)
    assert job.due_at == (
        now if age >= NEGATIVE_SCAN_TTL else now + timedelta(minutes=10)
    )


def test_legacy_continuation_without_time_does_not_issue_fresh_negative_evidence(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", False)
    progress, args = start_scan(session, sync_seed)
    progress.pop("scan_evidence_at", None)
    now = START + timedelta(hours=7)
    assert refresh_material_links(session, now=now, **args)
    scan = session.get(
        MaterialLinkScan, (sync_seed.tenant_id, "acc-1", _scan_scope(D, D, ()))
    )
    assert scan is None
    assert "scan_evidence_at" not in progress
    job = session.get(ReportSyncJob, request(session, sync_seed, D))
    job.progress = dict(progress)
    assert wait_for_dependencies(session, job, now=now)
    assert job.due_at == now
    assert "scan_evidence_valid" not in job.progress


def test_legacy_continuation_does_not_overwrite_other_valid_scan(session, sync_seed):
    progress, args = start_scan(session, sync_seed)
    progress.pop("scan_evidence_at")
    observed = START + timedelta(minutes=10)
    signature = progress["scan_evidence_signature"]
    scan = MaterialLinkScan(
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        query_scope=_scan_scope(D, D, ()),
        scanned_at=observed,
        unresolved_ids=["missing"],
        evidence_signature=signature,
    )
    session.add(scan)
    session.flush()
    assert refresh_material_links(session, now=observed + timedelta(minutes=5), **args)
    session.refresh(scan)
    assert scan.scanned_at == observed
    assert scan.evidence_signature == signature


def test_real_job_keeps_positive_progress_with_fifteen_minute_queue_delays(
    committed_sync_seed, monkeypatch
):
    from sqlmodel import select

    from app.modules.reporting import jobs
    from app.modules.reporting.store_models import MaterialPostLink
    from tests.modules.reporting.test_material_link_resume import (
        SlowReader,
        seed_job,
        step,
    )

    seed, factory = committed_sync_seed
    job_id, reader = seed_job(factory, seed), SlowReader()
    clock = [START]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr(jobs, "datetime", Clock)
    for _ in range(60):
        result = step(factory, job_id, reader)
        assert reader.calls <= 1
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            assert job.attempts == 0
            if job.error_code == "material_directory_pending":
                assert result == "WAITING"
                break
            assert job.error_code is None
        clock[0] += timedelta(minutes=15)
    else:
        raise AssertionError("长排队的持久分页未完成定位")
    assert len(reader.history) == len(set(reader.history))
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert (link.ad_remote_id, link.platform_material_id) == ("ad199", None)
