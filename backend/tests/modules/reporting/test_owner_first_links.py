"""宽账户只补实际缺口，不以整账户百页概览阻挡可精确定位的旧帖。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.ads.models import AdObject
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_material_link_resume import SlowReader, seed_job, step
from tests.modules.reporting.test_material_query import D


def wide_job(factory, seed, existing_page=None):
    job_id = seed_job(factory, seed)
    with factory() as session:
        publish_ad_day(
            session,
            tenant_id=seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
            start=D,
            end=D,
            rows=[ad_row(f"ad{i:03}", D, adv="acc-1") for i in range(201)],
        )
        session.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="ad",
                remote_id="ad199",
                ad_type="SMART_PLUS",
                published_version=1,
                observed_at=datetime.now(UTC),
                configuration={
                    "creative_list": [
                        {
                            "creative_info": {
                                "identity_type": "BC_AUTH_TT",
                                "identity_id": "identity",
                                "identity_authorized_bc_id": "bc-a",
                                "video_info": {"video_id": "video199"},
                            }
                        }
                    ]
                },
            )
        )
        if existing_page:
            session.get(ReportSyncJob, job_id).progress = {
                "overview_ad_ids": [],
                "next_page": existing_page,
            }
        session.commit()

    return job_id


@pytest.mark.parametrize("existing_page", [None, 27])
def test_wide_history_resolves_from_owner_and_content_without_full_overview(
    committed_sync_seed, existing_page
):
    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed, existing_page)

    class Reader(SlowReader):
        def material_ad_links(self, **kwargs):
            raise AssertionError("可定位并有精确内容证据时不应先扫描整个账户")

        def material_video_content(self, **kwargs):
            self.tick(("assets",))
            return [{"video_id": "video199", "signature": "a" * 32, "size": 123}]

        def spark_video_content(self, **kwargs):
            self.tick(("content",))
            return {"signature": "a" * 32, "size": 123}

    reader = Reader()
    for _ in range(30):
        outcome = step(factory, job_id, reader)
        assert reader.calls <= 1
        if outcome == "COMPLETE":
            break
        with factory() as session:
            assert session.get(ReportSyncJob, job_id).error_code is None
    else:
        raise AssertionError("精确路径必须有界完成")
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert (link.ad_remote_id, link.platform_material_id) == ("ad199", "video199")


def test_owner_lookup_failure_keeps_overview_and_does_not_cache_false_absence(
    committed_sync_seed,
):
    from app.integrations.tiktok.contracts.report_reader import RawReportPage
    from app.modules.reporting.store_models import MaterialLinkScan
    from tests.modules.reporting.test_material_links import overview

    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed)

    class Reader(SlowReader):
        def material_ad_posts(self, **kwargs):
            self.tick(("posts", tuple(kwargs["ad_ids"])))
            return RawReportPage((), 1, 0, 0)

        def material_ad_links(self, **kwargs):
            self.tick(("overview", kwargs["page"]))
            return overview(("ad199", "old-post", "material199", "same.mp4"))

    reader = Reader()
    for _ in range(2):
        assert step(factory, job_id, reader) == "WAITING"
        assert reader.calls == 1
        with factory() as session:
            assert not session.exec(select(MaterialLinkScan)).all()
    assert step(factory, job_id, reader) == "COMPLETE"
    assert reader.history[-1] == ("overview", 1)
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert link.platform_material_id == "video199"


def test_new_owner_waits_for_its_directory_without_scanning_whole_account(
    committed_sync_seed,
):
    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed)
    with factory() as session:
        session.delete(session.get(AdObject, (seed.tenant_id, "acc-1", "ad", "ad199")))
        session.commit()

    class Reader(SlowReader):
        def material_ad_links(self, **kwargs):
            raise AssertionError("缺目录时应交给原目录依赖链，不读取整账户概览")

    reader = Reader()
    for _ in range(30):
        assert step(factory, job_id, reader) == "WAITING"
        assert reader.calls <= 1
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            if job.error_code == "material_directory_pending":
                break
            assert job.error_code is None
    else:
        raise AssertionError("归属定位后必须正确进入目录依赖")
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert link.ad_remote_id == "ad199" and link.platform_material_id is None


@pytest.mark.parametrize("existing_page", [None, 64])
def test_known_missing_owner_limits_overview_and_freezes_its_pages(
    committed_sync_seed, existing_page
):
    from app.modules.reporting.jobs import _material_link_overview_ads
    from tests.modules.reporting.test_material_query import material_row, publish

    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed, existing_page)
    with factory() as session:
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="old-post",
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id="ad199",
                observed_at=datetime.now(UTC),
            )
        )
        session.flush()
        job = session.get(ReportSyncJob, job_id)
        if existing_page:
            job.progress = job.progress | {"owner_lookup_first": True}
        assert _material_link_overview_ads(session, job) == ("ad199",)
        assert job.progress["next_page"] == 1
        job.progress = job.progress | {"next_page": 2}
        publish(
            session,
            seed,
            "acc-1",
            [
                material_row("old-post", "5", "same.mp4"),
                material_row("new-post", "1", "new.mp4"),
            ],
        )
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="new-post",
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id="ad001",
                observed_at=datetime.now(UTC),
            )
        )
        session.flush()
        assert _material_link_overview_ads(session, job) == ("ad199",)
        assert job.progress["next_page"] == 2


def test_known_owner_must_not_narrow_away_an_unknown_post(committed_sync_seed):
    from app.modules.reporting.jobs import _material_link_overview_ads
    from tests.modules.reporting.test_material_query import material_row, publish

    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed, 64)
    with factory() as session:
        publish(
            session,
            seed,
            "acc-1",
            [
                material_row("old-post", "5", "same.mp4"),
                material_row("unknown", "1", "other.mp4"),
            ],
        )
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="old-post",
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id="ad199",
                observed_at=datetime.now(UTC),
            )
        )
        session.flush()
        job = session.get(ReportSyncJob, job_id)
        assert _material_link_overview_ads(session, job) == ()
        assert job.progress["next_page"] == 64
        assert job.progress["owner_lookup_first"] is True


def test_known_owner_refreshes_exact_bridge_without_full_scan(committed_sync_seed):
    from tests.modules.reporting.test_material_links import overview

    seed, factory = committed_sync_seed
    job_id = wide_job(factory, seed, 64)
    with factory() as session:
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id="old-post",
                main_material_type="VIDEO_SPARK_ADS",
                ad_remote_id="ad199",
                observed_at=datetime.now(UTC),
            )
        )
        job = session.get(ReportSyncJob, job_id)
        job.progress = job.progress | {"owner_lookup_first": True}
        session.commit()

    class Reader(SlowReader):
        def material_video_content(self, **kwargs):
            self.tick(("assets",))
            return []

        def spark_video_content(self, **kwargs):
            self.tick(("content",))
            return None

        def material_ad_links(self, *, advertiser_id, page, ad_ids=()):
            assert advertiser_id == "acc-1" and ad_ids == ("ad199",) and page == 1
            self.tick(("overview", page))
            return overview(("ad199", "old-post", "material199", "same.mp4"))

    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.calls == 1
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.calls == 1
    assert step(factory, job_id, reader) == "COMPLETE"
    assert reader.calls == 1
    with factory() as session:
        link = session.exec(
            select(MaterialPostLink).where(
                MaterialPostLink.tenant_id == seed.tenant_id,
                MaterialPostLink.main_material_id == "old-post",
            )
        ).one()
        assert link.platform_material_id == "video199"
