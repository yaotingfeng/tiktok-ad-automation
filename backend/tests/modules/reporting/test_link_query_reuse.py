"""旧帖定位必须复用完整查询，并对最终广告取得直接证据。"""

from datetime import timedelta

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.material_links import _locate_post_step
from tests.modules.reporting.test_material_links import NOW, D


def state(mid, count=32):
    return {
        "mid": mid,
        "candidates": [f"ad{i:02}" for i in range(count)],
        "next_batch": 0,
        "search_stack": [],
    }


def page(posts, number=1, pages=1, total=None):
    return RawReportPage(
        tuple({"dimensions": {"main_material_id": p}} for p in posts),
        number,
        pages,
        len(posts) if total is None else total,
    )


def test_many_posts_reuse_completed_queries_and_verify_last_ad():
    from app.modules.reporting.link_queries import QueryMembershipReader

    posts = [f"old{i:02}" for i in range(30)]
    cache, calls, owners = {}, [], {}

    def read(ads, first, last, number):
        calls.append((ads, first, last, number))
        return page(posts if "ad31" in ads else [])

    for post in posts:
        cursor = state(post)
        for _ in range(20):
            reader = QueryMembershipReader(read, cache, targets=posts, now=NOW)
            complete, owner = _locate_post_step(
                cursor, read_ad_posts=reader, first=D, last=D
            )
            if complete:
                owners[post] = owner
                break
    assert owners == dict.fromkeys(posts, "ad31")
    assert len(calls) == 7
    assert calls[-1][0] == ("ad31",)


def test_disappearing_target_is_not_assigned_to_unread_right_ad():
    calls = []

    def read(ads, _first, _last, _number):
        calls.append(ads)
        return page(["p"] if len(ads) == 2 else [])

    cursor = state("p", 2)
    with pytest.raises(DomainError) as caught:
        for _ in range(8):
            _locate_post_step(cursor, read_ad_posts=read, first=D, last=D)
    assert caught.value.code == "report_snapshot_changed"
    assert calls == [("ad00", "ad01"), ("ad00",), ("ad01",)]


def test_query_cache_requires_complete_pages_exact_dates_targets_and_ttl():
    from app.modules.reporting.link_queries import QueryMembershipReader

    cache, calls = {}, []

    def read(_ads, first, _last, number):
        calls.append((first, number))
        return page([f"p{number}"], number, 2, 2)

    def reader(targets=("p1", "p2"), now=NOW):
        return QueryMembershipReader(read, cache, targets=targets, now=now)

    reader()(("a",), D, D, 1)
    reader()(("a",), D, D, 2)
    assert len(reader()(("a",), D, D, 1).rows) == 2
    assert len(calls) == 2
    reader()(("a",), D + timedelta(days=1), D + timedelta(days=1), 1)
    reader(targets=("p1", "p2", "p3"))(("a",), D, D, 1)
    reader()(("a",), D, D, 2)
    reader(now=NOW + timedelta(minutes=6))(("a",), D, D, 1)
    assert len(calls) == 6


@pytest.mark.parametrize("overlap", [False, True])
def test_changed_or_overlapping_pages_cannot_form_negative_cache(overlap):
    from app.modules.reporting.link_queries import QueryMembershipReader

    cache = {}

    def read(_ads, _first, _last, number):
        if number == 1:
            return page(["p1"], 1, 2, 2)
        return page(["p1" if overlap else "p2"], 2, 2, 2 if overlap else 3)

    reader = QueryMembershipReader(read, cache, targets=["missing"], now=NOW)
    reader(("a",), D, D, 1)
    with pytest.raises(DomainError) as caught:
        reader(("a",), D, D, 2)
    assert caught.value.code == "report_snapshot_changed"
    assert not cache.get("entries")


def test_negative_membership_is_shared_but_eviction_requires_reading_again(monkeypatch):
    from app.modules.reporting import link_queries

    monkeypatch.setattr(link_queries, "MAX_ENTRIES", 1)
    cache, calls = {}, []

    def read(ads, _first, _last, _number):
        calls.append(ads)
        return page([])

    reader = link_queries.QueryMembershipReader(
        read, cache, targets=["p1", "p2"], now=NOW
    )
    for target in ("p1", "p2"):
        assert _locate_post_step(
            state(target), read_ad_posts=reader, first=D, last=D
        ) == (True, None)
    assert len(calls) == 1
    reader(("other",), D, D, 1)
    reader(tuple(state("p1")["candidates"]), D, D, 1)
    assert len(calls) == 3


def test_cache_byte_bound_does_not_truncate_membership(monkeypatch):
    from app.modules.reporting import link_queries

    monkeypatch.setattr(link_queries, "MAX_CACHE_BYTES", 80)
    cache, calls = {}, []

    def read(*args):
        calls.append(args)
        return page(["p1", "p2"])

    reader = link_queries.QueryMembershipReader(
        read, cache, targets=["p1", "p2"], now=NOW
    )
    assert len(reader(("a",), D, D, 1).rows) == 2
    assert not cache["entries"]
    assert len(reader(("a",), D, D, 1).rows) == 2
    assert len(calls) == 2


def test_too_many_targets_disables_reuse_without_resetting_partial_pages(monkeypatch):
    from app.modules.reporting import link_queries

    monkeypatch.setattr(link_queries, "MAX_TARGETS", 1)
    cache = {}

    def read(_ads, _first, _last, number):
        return page([f"p{number}"], number, 2, 2)

    for number in (1, 2):
        reader = link_queries.QueryMembershipReader(
            read, cache, targets=["p1", "p2"], now=NOW
        )
        assert reader(("a",), D, D, number).page == number
    assert not cache["entries"] and "pending" not in cache


def test_pending_evidence_has_hard_byte_bound(monkeypatch):
    from app.modules.reporting import link_queries

    monkeypatch.setattr(link_queries, "MAX_STATE_BYTES", 100)
    cache = {}
    reader = link_queries.QueryMembershipReader(
        lambda *_args: page(["p1"], 1, 2, 2), cache, targets=["p1"], now=NOW
    )
    with pytest.raises(DomainError) as caught:
        reader(("a",), D, D, 1)
    assert caught.value.code == "report_truncation_risk"
    assert not cache["entries"]


def test_continuous_page_drift_exhausts_real_job_retry_budget(committed_sync_seed):
    from sqlmodel import select

    from app.modules.reporting.jobs import MAX_PROVIDER_RETRIES
    from app.modules.reporting.store_models import (
        MaterialLinkScan,
        MaterialPostLink,
        ReportSyncJob,
    )
    from tests.modules.reporting.test_material_link_resume import (
        SlowReader,
        seed_job,
        step,
    )

    seed, factory = committed_sync_seed
    job_id = seed_job(factory, seed)

    class DriftingReader(SlowReader):
        def material_ad_posts(self, *, advertiser_id, ad_ids, start, end, page):
            self.tick((tuple(ad_ids), page))
            return RawReportPage(
                ({"dimensions": {"main_material_id": f"noise-{page}"}},),
                page,
                2,
                2 if page == 1 else 3,
            )

    reader = DriftingReader()
    for _ in range(30):
        if step(factory, job_id, reader) == "FAILED":
            break
    else:
        pytest.fail("持续分页变化必须消耗有限重试，不能无限续步")
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.attempts == MAX_PROVIDER_RETRIES
        assert job.error_code == "report_snapshot_changed"
        assert not session.exec(select(MaterialPostLink)).all()
        assert not session.exec(select(MaterialLinkScan)).all()
