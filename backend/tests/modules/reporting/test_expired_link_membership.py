"""过期完整成员只用于阳性缩小，不能证明缺失或替代最终单广告证据。"""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import link_queries
from app.modules.reporting.link_queries import (
    QueryMembershipReader,
    material_identity_key,
)
from app.modules.reporting.material_links import (
    _locate_post_step,
    refresh_material_links,
)
from app.modules.reporting.store_models import MaterialLinkScan, MaterialPostLink
from tests.modules.reporting.test_link_query_reuse import page, state
from tests.modules.reporting.test_material_link_resume import seed_job
from tests.modules.reporting.test_material_links import NOW, D

TARGET = material_identity_key("old-post", "VIDEO_SPARK_ADS")
OTHER = material_identity_key("other", "VIDEO_SPARK_ADS")
ADS = ("ad000", "ad001", "ad002", "ad003")
OLD = NOW - timedelta(minutes=30)


def completed_cache(queries, *, posts=("old-post",), targets=(TARGET, OTHER)):
    cache = {}
    reader = QueryMembershipReader(
        lambda *_: page(posts), cache, targets=targets, now=OLD
    )
    for ads in queries:
        reader(ads, D, D, 1)
    return cache


def test_expired_positive_returns_only_current_target_without_renewing_or_touching_pending():
    cache = completed_cache([ADS], posts=("old-post", "other"))
    # 另一集合尚未结束的真实分页不能被阳性提示改写或完成。
    cache["pending"] = {
        "key": "another-query",
        "at": OLD.timestamp(),
        "next_page": 2,
        "signature": [3, 3],
        "seen": ["first-page"],
        "members": [OTHER],
    }
    before = deepcopy(cache)
    reader = QueryMembershipReader(
        lambda *_: pytest.fail("expired positive should avoid the group request"),
        cache,
        targets=(TARGET, OTHER),
        now=NOW,
    )
    result = reader(ADS, D, D, 1, positive_target=TARGET)
    assert result == page(["old-post"])
    assert cache == before


@pytest.mark.parametrize(
    "case", ["missing", "negative", "single", "other_type", "new_target", "dates"]
)
def test_expired_nonpositive_or_leaf_must_read_provider(case):
    ads = ADS[:1] if case == "single" else ADS
    target = (
        material_identity_key("old-post", "CAROUSEL_SPARK_ADS")
        if case == "other_type"
        else TARGET
    )
    targets = (TARGET, OTHER, target)
    cache = completed_cache(
        [ads],
        posts=()
        if case == "negative"
        else ("other",)
        if case in {"missing", "new_target"}
        else ("old-post",),
        targets=targets if case != "new_target" else (OTHER,),
    )
    calls = []

    def read(*args):
        calls.append(args)
        return page([])

    reader = QueryMembershipReader(read, cache, targets=targets, now=NOW)
    if case == "new_target":
        assert not cache["entries"]
    day = D + timedelta(days=1) if case == "dates" else D
    assert reader(ads, day, day, 1, positive_target=target).rows == ()
    assert calls == [(ads, day, day, 1)]


def test_expired_entry_cannot_replace_later_page_or_renew_completed_observation():
    cache = completed_cache([ADS])
    calls = []

    def read(_ads, _first, _last, number):
        calls.append(number)
        return page(["old-post" if number == 1 else "other"], number, 2, 2)

    reader = QueryMembershipReader(read, cache, targets=(TARGET, OTHER), now=NOW)
    # 未声明当前目标的调用不可借过期阳性，实际启动新分页。
    reader(ADS, D, D, 1)
    started = cache["pending"]["at"]
    reader = QueryMembershipReader(
        read, cache, targets=(TARGET, OTHER), now=NOW + timedelta(minutes=10)
    )
    assert reader(ADS, D, D, 2, positive_target=TARGET).page == 2
    assert calls == [1, 2]
    assert "pending" not in cache
    assert next(iter(cache["entries"].values()))["at"] == started


def test_expired_entries_without_remaining_positive_are_removed():
    cache = completed_cache([ADS], posts=("other",))
    QueryMembershipReader(lambda *_: None, cache, targets=(TARGET,), now=NOW)
    assert cache["entries"] == {}


@pytest.mark.parametrize("expired", [False, True])
def test_large_pending_preserves_small_complete_entries_without_negative_promotion(
    expired,
):
    cache = completed_cache([ADS])
    cache["pending"] = {
        "key": "another-query",
        "at": OLD.timestamp(),
        "next_page": 91,
        "signature": [190, 19_000],
        "seen": [f"{index:064x}" for index in range(9000)],
        "members": [],
    }
    assert (
        link_queries.MAX_CACHE_BYTES
        < len(json.dumps(cache).encode())
        < link_queries.MAX_STATE_BYTES
    )
    before = deepcopy(cache)
    calls = []

    def read(*args):
        calls.append(args)
        return page(["unrelated"], 1, 2, 2)

    reader = QueryMembershipReader(
        read,
        cache,
        targets=(TARGET, OTHER),
        now=NOW if expired else OLD + timedelta(seconds=60),
    )
    assert cache == before
    assert reader(ADS, D, D, 1, positive_target=TARGET) == page(["old-post"])
    assert not calls and cache == before
    if expired:
        # 未命中仍真实分页；不能把旧阳性条目或另一查询的部分页提升成新负缓存。
        assert reader(ADS, D, D, 1, positive_target=OTHER).next_page == 2
        assert calls == [(ADS, D, D, 1)]
        assert cache["entries"] == before["entries"]


@pytest.mark.parametrize("limit", ["MAX_ENTRIES", "MAX_CACHE_BYTES"])
def test_retained_positive_cache_still_obeys_capacity(monkeypatch, limit):
    cache = completed_cache([ADS, ADS[:2]])
    monkeypatch.setattr(link_queries, limit, 1 if limit == "MAX_ENTRIES" else 80)
    QueryMembershipReader(lambda *_: None, cache, targets=(TARGET, OTHER), now=NOW)
    assert len(cache["entries"]) <= (1 if limit == "MAX_ENTRIES" else 0)


def progress_for(cache, *, confirmed=False):
    return {
        "typed_link_search": True,
        "owner_lookup_first": True,
        "overview_complete": True,
        "scan_evidence_ids": ["old-post"],
        "scan_evidence_at": NOW.isoformat(),
        "locate_queries": cache,
        "locate_post": {
            "key": TARGET,
            "mid": "old-post",
            "mtype": "VIDEO_SPARK_ADS",
            "candidates": list(ADS),
            "next_batch": len(ADS),
            "search_stack": [{"ads": list(ADS), "confirmed": confirmed}],
        },
    }


def refresh(db, seed, progress, read):
    return refresh_material_links(
        db,
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda *_: pytest.fail("overview already complete"),
        read_ad_posts=read,
        now=NOW,
        start=D,
        end=D,
        progress=progress,
    )


def test_expired_group_positives_reduce_real_reads_but_final_owner_is_fresh(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    cache = completed_cache([ADS, ADS[:2], ADS[:1]], targets=(TARGET,))
    progress = progress_for(cache)
    calls = []

    def read(ads, *_):
        calls.append(ads)
        return page(["old-post"] if ADS[0] in ads else [])

    for _ in range(8):
        with factory() as db:
            complete = refresh(db, seed, progress, read)
            db.commit()
        if complete:
            break
    assert complete
    assert calls == [ADS[:1]]  # 原流程需读 4 广告、2 广告、1 广告共三次。
    with factory() as db:
        owner = db.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert owner.ad_remote_id == ADS[0]


@pytest.mark.parametrize("left_owner", [False, True])
def test_nested_truncation_preserves_parent_positive_until_owner_or_contradiction(
    committed_sync_seed, left_owner
):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    progress = progress_for({}, confirmed=True)
    calls = []

    def read(ads, *_):
        calls.append(ads)
        if len(ads) > 1:
            return RawReportPage((), 1, 200, 20_000)
        return page(["old-post"] if left_owner and ads == ADS[:1] else [])

    error = None
    for _ in range(16):
        with factory() as db:
            try:
                complete = refresh(db, seed, progress, read)
                db.commit()
            except DomainError as caught:
                db.rollback()
                error = caught
                break
        if complete:
            break
    with factory() as db:
        owners = db.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).all()
        if left_owner:
            assert error is None and complete
            assert [owner.ad_remote_id for owner in owners] == [ADS[0]]
            assert not any(ADS[2] in ads for ads in calls)
        else:
            assert error is not None and error.code == "report_snapshot_changed"
            assert not owners
            assert (
                db.exec(
                    select(MaterialLinkScan).where(
                        MaterialLinkScan.tenant_id == seed.tenant_id,
                    )
                ).first()
                is None
            )


def test_prior_page_positive_survives_truncation_without_confirmed_node():
    cursor = state("p", 4)
    cursor["next_batch"] = 4
    cursor["search_stack"] = [{"ads": cursor["candidates"], "confirmed": False}]
    cursor["query"] = {"page": 2, "found": True, "signature": [200, 19_999]}

    def read(_ads, _first, _last, number):
        return RawReportPage((), number, 200, 20_000) if number == 2 else page([])

    with pytest.raises(DomainError, match="父子集合不一致"):
        for _ in range(12):
            complete, _ = _locate_post_step(cursor, read_ad_posts=read, first=D, last=D)
            if complete:
                pytest.fail("prior positive cannot become a completed negative scan")


def test_truncated_actual_single_ad_fails_without_an_extra_split():
    cursor = state("p", 2)
    cursor["next_batch"] = 2
    cursor["search_stack"] = [{"ads": cursor["candidates"], "confirmed": True}]
    with pytest.raises(DomainError) as caught:
        _locate_post_step(
            cursor,
            read_ad_posts=lambda *_: RawReportPage((), 1, 200, 20_000),
            first=D,
            last=D,
        )
    assert caught.value.code == "report_truncation_risk"
