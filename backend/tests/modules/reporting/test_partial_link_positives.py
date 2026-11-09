"""未完成分页只复用阳性，阴性和最终归属仍须完整/单广告证据。"""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlmodel import select

from app.modules.reporting.link_queries import QueryMembershipReader
from app.modules.reporting.store_models import MaterialPostLink
from tests.modules.reporting.test_expired_link_membership import (
    ADS,
    NOW,
    OLD,
    OTHER,
    TARGET,
    D,
    progress_for,
    refresh,
)
from tests.modules.reporting.test_link_query_reuse import page
from tests.modules.reporting.test_material_link_resume import seed_job


def partial_cache(queries=(ADS,), *, targets=(TARGET, OTHER)):
    cache = {}
    reader = QueryMembershipReader(
        lambda *_: page(["old-post", "other"], 1, 2, 3),
        cache,
        targets=targets,
        now=OLD,
    )
    for ads in queries:
        reader(ads, D, D, 1)
        cache.pop("pending", None)  # 当前目标已命中，原定位器放弃其余页。
    return cache


def test_partial_positive_survives_abandoned_pagination_without_complete_entry():
    cache = partial_cache()
    assert cache["entries"] and all(
        key.startswith("positive:") for key in cache["entries"]
    )
    before = deepcopy(cache)
    reader = QueryMembershipReader(
        lambda *_: pytest.fail("known group positive should avoid rereading"),
        cache,
        targets=(TARGET, OTHER),
        now=NOW,
    )
    assert reader(ADS, D, D, 1, positive_target=OTHER) == page(["other"])
    assert cache == before and "pending" not in cache


@pytest.mark.parametrize("case", ["missing", "single", "dates", "untyped", "generic"])
def test_partial_positive_never_supplies_negative_or_singleton(case):
    cache = partial_cache()
    target = "unseen" if case == "missing" else TARGET
    ads = ADS[:1] if case == "single" else ADS
    day = D + timedelta(days=1) if case == "dates" else D
    if case == "untyped":
        target = TARGET.replace("VIDEO_SPARK_ADS", "CAROUSEL_SPARK_ADS")
    calls = []

    def read(*args):
        calls.append(args)
        return page([])

    reader = QueryMembershipReader(read, cache, targets=(TARGET, OTHER), now=NOW)
    assert (
        reader(
            ads,
            day,
            day,
            1,
            positive_target=None if case == "generic" else target,
        ).rows
        == ()
    )
    assert calls == [(ads, day, day, 1)]


def test_partial_group_positives_save_real_reads_but_owner_still_fresh(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    progress = progress_for(partial_cache((ADS, ADS[:2]), targets=(TARGET,)))
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
    assert complete and calls == [ADS[:1]]
    with factory() as db:
        owners = db.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).all()
        assert [owner.ad_remote_id for owner in owners] == [ADS[0]]


def test_complete_fresh_negative_overrides_older_partial_positive():
    cache = partial_cache()
    reader = QueryMembershipReader(
        lambda *_: page([]), cache, targets=(TARGET, OTHER), now=NOW
    )
    reader(ADS, D, D, 1)  # 不声明阳性目标，真实读取新完整集合。
    reader = QueryMembershipReader(
        lambda *_: pytest.fail("fresh complete evidence should be reused"),
        cache,
        targets=(TARGET, OTHER),
        now=NOW,
    )
    assert reader(ADS, D, D, 1, positive_target=TARGET).rows == ()
    assert not any(key.startswith("positive:") for key in cache["entries"])


def test_partial_positive_does_not_replace_next_page_or_new_target_scope():
    cache = partial_cache()
    calls = []

    def read(*args):
        calls.append(args)
        return (
            page(["old-post"], 1, 2, 2) if args[-1] == 1 else page(["other"], 2, 2, 2)
        )

    reader = QueryMembershipReader(read, cache, targets=(TARGET, OTHER), now=NOW)
    reader(ADS, D, D, 1)
    assert reader(ADS, D, D, 2, positive_target=TARGET).page == 2
    assert len(calls) == 2
    reader = QueryMembershipReader(
        read, cache, targets=(TARGET, OTHER, "new-target"), now=NOW
    )
    assert cache["entries"] == {}


@pytest.mark.parametrize("limit", ["MAX_ENTRIES", "MAX_CACHE_BYTES"])
def test_partial_positives_share_existing_capacity(monkeypatch, limit):
    import json

    from app.modules.reporting import link_queries

    cache = partial_cache((ADS, ADS[:2], ADS[2:]))
    monkeypatch.setattr(link_queries, limit, 1 if limit == "MAX_ENTRIES" else 220)
    QueryMembershipReader(lambda *_: None, cache, targets=(TARGET, OTHER), now=NOW)
    assert len(cache["entries"]) <= 1
    if limit == "MAX_CACHE_BYTES":
        assert len(json.dumps(cache).encode()) <= 220


def test_partial_positive_contradiction_never_writes_owner_or_negative_scan(
    committed_sync_seed,
):
    from app.core.errors import DomainError
    from app.modules.reporting.store_models import MaterialLinkScan

    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    progress = progress_for(partial_cache((ADS, ADS[:2]), targets=(TARGET,)))
    error = None
    for _ in range(12):
        with factory() as db:
            try:
                complete = refresh(db, seed, progress, lambda *_: page([]))
                db.commit()
            except DomainError as caught:
                db.rollback()
                error = caught
                break
        if complete:
            break
    assert error is not None and error.code == "report_snapshot_changed"
    with factory() as db:
        assert not db.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
            )
        ).all()
        assert not db.exec(
            select(MaterialLinkScan).where(
                MaterialLinkScan.tenant_id == seed.tenant_id,
            )
        ).all()


def test_partial_positive_preserves_existing_cursor_validation_contract():
    from app.modules.sync_control.report_demand import _valid_link_continuation_progress

    cache = partial_cache()
    progress = progress_for(cache)
    assert _valid_link_continuation_progress(progress)
    assert set(cache) == {"typed", "targets", "entries"}
    assert all(
        key.startswith("positive:") and len(key) == 73 for key in cache["entries"]
    )
    assert all(set(value) == {"at", "members"} for value in cache["entries"].values())
