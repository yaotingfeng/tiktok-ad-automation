"""已失去完整缓存复用价值的查询，正向命中可推进；否定仍须完整分页。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.reporting.link_queries import (
    QueryMembershipReader,
    material_identity_key,
)
from app.modules.reporting.material_links import refresh_material_links
from app.modules.reporting.store_models import MaterialLinkScan, MaterialPostLink
from tests.modules.reporting.test_link_query_reuse import page
from tests.modules.reporting.test_material_link_resume import seed_job
from tests.modules.reporting.test_material_query import D


@pytest.mark.parametrize(
    "found,age,advance",
    [(True, 360, True), (True, 60, False), (False, 360, False)],
)
def test_positive_expired_query_can_advance_without_reading_remaining_pages(
    committed_sync_seed, found, age, advance
):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    now = datetime.now(UTC)
    cache = {}
    key = material_identity_key("old-post", "VIDEO_SPARK_ADS")
    ads = ("ad000", "ad001")
    first = QueryMembershipReader(
        lambda *_: page(["old-post" if found else "other-1"], 1, 3, 3),
        cache,
        targets=[key],
        now=now - timedelta(seconds=age),
    )
    first(ads, D, D, 1)
    calls = []
    progress = {
        "typed_link_search": True,
        "owner_lookup_first": True,
        "overview_complete": True,
        "scan_evidence_ids": ["old-post"],
        "locate_queries": cache,
        "locate_post": {
            "key": key,
            "mid": "old-post",
            "mtype": "VIDEO_SPARK_ADS",
            "candidates": list(ads),
            "next_batch": 2,
            "search_stack": [{"ads": list(ads), "confirmed": False}],
            "query": {"page": 2, "found": found, "signature": [3, 3]},
        },
    }

    def read(current_ads, first_day, last_day, number):
        calls.append((current_ads, first_day, last_day, number))
        return page(["other-2"], 2, 3, 3)

    def overview(_number):
        pytest.fail("private owner lookup must not restart a full overview")

    def refresh(db, post_reader):
        return refresh_material_links(
            db,
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            read_overview=overview,
            read_ad_posts=post_reader,
            now=now,
            start=D,
            end=D,
            progress=progress,
        )

    with factory() as db:
        assert not refresh(db, read)
        db.commit()
    assert calls == [(ads, D, D, 2)]
    current = progress["locate_post"]
    if advance:
        assert "query" not in current
        assert current["search_stack"][-1]["confirmed"]
        assert "pending" not in cache
    else:
        assert current["query"]["page"] == 3
        assert cache["pending"]["at"] == first.now
    assert not cache["entries"]  # 部分查询绝不产生可签发缺失的完整成员缓存。
    if advance:
        # 父页的旧正向证据不能替代最终单广告读取。两个子广告均未命中时，
        # 仍按快照矛盾退出，不能猜owner或发布整轮负扫描证据。
        def empty(*_):
            return page([], 1, 0, 0)

        with factory() as db:
            assert not refresh(db, empty)
            db.commit()
        with factory() as db:
            with pytest.raises(DomainError) as caught:
                refresh(db, empty)
            assert caught.value.code == "report_snapshot_changed"
            db.rollback()
            assert (
                db.exec(
                    select(MaterialPostLink).where(
                        MaterialPostLink.tenant_id == seed.tenant_id,
                        MaterialPostLink.main_material_id == "old-post",
                    )
                ).first()
                is None
            )
            assert (
                db.exec(
                    select(MaterialLinkScan).where(
                        MaterialLinkScan.tenant_id == seed.tenant_id
                    )
                ).first()
                is None
            )
