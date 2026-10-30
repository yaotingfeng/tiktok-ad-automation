"""真实 PG：轮播归属和视频身份分别完成，旧任务无需清队列。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.reporting import material_links, planner
from app.modules.reporting.directory_refresh import request_link_directory
from app.modules.reporting.health import measure_coverage
from app.modules.reporting.jobs import _material_link_overview_ads
from app.modules.reporting.link_queries import material_identity_key
from app.modules.reporting.link_work import wake_link_dependencies
from app.modules.reporting.store_models import AdSyncBc, MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_material_asset_identity import seed_identity
from tests.modules.reporting.test_material_link_resume import step
from tests.modules.reporting.test_material_links import overview
from tests.modules.reporting.test_material_query import D, material_row, publish
from tests.modules.reporting.test_material_value import fact

NOW = datetime.combine(D, datetime.min.time(), tzinfo=UTC)
CAROUSELS = ("CAROUSEL_SPARK_ADS", "CAROUSEL_NON_SPARK_ADS")


def seed_posts(session, seed, facts, links):
    rows = []
    for mid, kind in facts:
        row = material_row(mid, "5")
        row["dimensions"]["main_material_type"] = kind
        rows.append(row)
    publish(session, seed, "acc-1", rows)
    for mid, kind, ad, vid in links:
        session.add(
            MaterialPostLink(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                main_material_id=mid,
                main_material_type=kind,
                ad_remote_id=ad,
                platform_material_id=vid,
                observed_at=NOW,
            )
        )
    session.flush()
    return session.get(ReportSyncJob, request(session, seed, D))


def needs(session, seed):
    return material_links.needs_material_links(
        session,
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        start=D,
        end=D,
    )


def coverage(session, seed):
    planner.ensure_bc_plans(session, now=NOW, tenant_id=seed.tenant_id)
    plan = session.get(AdSyncBc, (seed.tenant_id, "bc-a"))
    return measure_coverage(session, plan, now=NOW, stage="material")


@pytest.mark.parametrize("kind", CAROUSELS)
def test_known_carousel_owner_finishes_without_video_or_directory(
    session, sync_seed, monkeypatch, kind
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = seed_posts(session, sync_seed, [("post", kind)], [("post", kind, "ad", None)])
    assert not needs(session, sync_seed)
    assert not material_links.wait_for_dependencies(session, job, now=NOW)
    assert request_link_directory(session, job, now=NOW) == 0
    metrics, issues, repair = coverage(session, sync_seed)
    assert metrics["unlinked_material_posts"] == 0
    assert not issues and not repair["links"]
    assert session.exec(select(MaterialPostLink)).one().platform_material_id is None
    assert not session.exec(select(AdDirectoryRun)).all()


@pytest.mark.parametrize("kind", CAROUSELS)
def test_carousel_without_owner_is_persisted_from_official_overview(
    session, sync_seed, kind
):
    job = seed_posts(session, sync_seed, [("post", kind)], [])
    assert needs(session, sync_seed)
    assert coverage(session, sync_seed)[0]["unlinked_material_posts"] == 1
    page = overview(("ad", "post", "native-inner", "carousel"))
    page.rows[0]["dimensions"]["main_material_type"] = kind
    assert material_links.refresh_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda _: page,
        read_ad_posts=lambda *_: pytest.fail("owner already proven"),
        now=NOW,
        start=D,
        end=D,
        progress=job.progress,
    )
    session.expire_all()
    link = session.exec(select(MaterialPostLink)).one()
    assert (link.ad_remote_id, link.main_material_type, link.ad_material_id) == (
        "ad",
        kind,
        "native-inner",
    )
    assert link.platform_material_id is None
    assert not needs(session, sync_seed)


def test_mixed_task_only_repairs_video_and_keeps_carousel_gap_separate(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    job = seed_posts(
        session,
        sync_seed,
        [("pictures", CAROUSELS[0]), ("movie", "VIDEO_SPARK_ADS")],
        [
            ("pictures", CAROUSELS[0], "carousel-ad", None),
            ("movie", "VIDEO_SPARK_ADS", "video-ad", None),
        ],
    )
    assert needs(session, sync_seed)
    assert coverage(session, sync_seed)[0]["unlinked_material_posts"] == 1
    _material_link_overview_ads(session, job)
    assert job.progress["overview_ad_ids"] == ["video-ad"]
    assert material_links.wait_for_dependencies(session, job, now=NOW)
    assert job.progress["waiting_ids"] == ["movie"]
    assert {
        tuple(run.query["ids"]) for run in session.exec(select(AdDirectoryRun)).all()
    } == {("video-ad",)}


@pytest.mark.parametrize(
    "kind,owner_kind,vid",
    [
        ("VIDEO_SPARK_ADS", CAROUSELS[0], "bad-vid"),
        (CAROUSELS[0], "VIDEO_SPARK_ADS", "video"),
        ("UNKNOWN_TYPE", "UNKNOWN_TYPE", "not-proof"),
    ],
)
def test_other_type_mapping_cannot_hide_gap(session, sync_seed, kind, owner_kind, vid):
    seed_posts(
        session, sync_seed, [("same-id", kind)], [("same-id", owner_kind, "ad", vid)]
    )
    assert needs(session, sync_seed)
    assert coverage(session, sync_seed)[0]["unlinked_material_posts"] == 1
    if kind != owner_kind:
        assert material_identity_key("same-id", kind) in material_links._unlinked(
            session, sync_seed.tenant_id, "acc-1", start=D, end=D
        )


@pytest.mark.parametrize("kind", CAROUSELS)
def test_legacy_waiting_carousel_completes_without_any_remote_read(
    committed_sync_seed, kind
):
    seed, factory = committed_sync_seed
    job_id = seed_identity(factory, seed, kind=kind)
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        job.status = "WAITING"
        job.due_at = datetime.now(UTC) + timedelta(hours=6)
        job.error_code = "material_directory_pending"
        job.progress = {"waiting_ids": ["0"], "dependency_attempts": 6}
        # 广告现在的图片不能回填为旧帖子的 VID。
        session.add(
            AdMaterialReference(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id="ad",
                platform_material_id="current-image",
                material_type="IMAGE",
                complete=True,
                published_version=10,
            )
        )
        session.commit()

    with factory() as session:
        now = datetime.now(UTC)
        assert wake_link_dependencies(session, now=now) == 1
        assert session.get(ReportSyncJob, job_id).due_at == now
        session.commit()

    class NoRemoteReads:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected remote read: {name}")

    assert step(factory, job_id, NoRemoteReads()) == "COMPLETE"
    with factory() as session:
        job = session.get(ReportSyncJob, job_id)
        assert job.status == "COMPLETE" and job.error_code is None
        assert job.request_count == 0
        assert session.exec(select(MaterialPostLink)).one().platform_material_id is None
        assert not needs(session, seed)


def test_other_namespace_negative_value_cannot_cancel_video_work(session, sync_seed):
    fact(session, sync_seed, "same-id", "5", "0")
    first = D - timedelta(days=1)
    correction = fact(session, sync_seed, "same-id", "-5", "0", day=first)
    correction.main_material_type = CAROUSELS[0]
    session.flush()
    assert material_links.needs_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        start=first,
        end=D,
    )
    assert (
        material_links._unlinked(
            session, sync_seed.tenant_id, "acc-1", start=first, end=D
        )[material_identity_key("same-id", "VIDEO_SPARK_ADS")][1]
        == "VIDEO_SPARK_ADS"
    )


@pytest.mark.parametrize("scope", ["account", "tenant", "empty_owner"])
def test_carousel_owner_must_be_nonempty_and_scoped(session, sync_seed, scope):
    from uuid import uuid4

    seed_posts(
        session,
        sync_seed,
        [("post", CAROUSELS[0])],
        [("post", CAROUSELS[0], "ad", None)],
    )
    link = session.exec(select(MaterialPostLink)).one()
    if scope == "account":
        link.advertiser_id = "acc-2"
    elif scope == "tenant":
        link.tenant_id = uuid4()
    else:
        link.ad_remote_id = ""
    session.flush()
    assert needs(session, sync_seed)
    assert coverage(session, sync_seed)[0]["unlinked_material_posts"] == 1


def test_corrected_type_does_not_inherit_another_namespaces_video(session, sync_seed):
    job = seed_posts(
        session,
        sync_seed,
        [("post", CAROUSELS[0])],
        [("post", "VIDEO_SPARK_ADS", "ad", "old-video")],
    )
    link = session.exec(select(MaterialPostLink)).one()
    link.ad_material_id = "old-video-inner"
    session.flush()
    page = overview(("ad", "post", None, "carousel"))
    page.rows[0]["dimensions"]["main_material_type"] = CAROUSELS[0]
    material_links.refresh_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda _: page,
        read_ad_posts=lambda *_: pytest.fail("owner proven"),
        now=NOW,
        start=D,
        end=D,
        progress=job.progress,
    )
    session.refresh(link)
    assert link.main_material_type == CAROUSELS[0]
    assert link.platform_material_id is None
    assert link.ad_material_id is None


@pytest.mark.parametrize("legacy", [False, True])
def test_wrong_type_breakdown_never_proves_carousel_owner(session, sync_seed, legacy):
    job = seed_posts(session, sync_seed, [("post", CAROUSELS[0])], [])
    # 仅用名称生成候选，身份证据必须来自带类型的单广告回执。
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="video-ad",
            platform_material_id="video",
            material_type="VIDEO",
            name="post",
            complete=True,
            published_version=1,
        )
    )
    session.flush()
    progress = {"overview_complete": True}
    if legacy:
        query_key = hashlib.sha256(
            json.dumps([["video-ad"], str(D), str(D)]).encode()
        ).hexdigest()
        progress.update(
            {
                "scan_evidence_ids": ["post"],
                "scan_evidence_at": NOW.isoformat(),
                "checked_ids": [],
                "locate_post": {
                    "mid": "post",
                    "candidates": ["video-ad"],
                    "next_batch": 1,
                    "search_stack": [{"ads": ["video-ad"], "confirmed": True}],
                    "query": {"page": 2, "found": True, "signature": [2, 2]},
                },
                "locate_queries": {
                    "targets": ["post"],
                    "entries": {},
                    "pending": {
                        "key": query_key,
                        "at": NOW.timestamp(),
                        "next_page": 2,
                        "signature": [2, 2],
                        "seen": [],
                        "members": ["post"],
                    },
                },
            }
        )

    def read(_ads, _start, _end, page):
        return RawReportPage(
            (
                {
                    "dimensions": {
                        "main_material_id": "post",
                        "main_material_type": "VIDEO_NON_SPARK_ADS",
                    },
                },
            ),
            page,
            2 if legacy and page == 2 else 1,
            2 if legacy and page == 2 else 1,
        )

    material_links.refresh_material_links(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        read_overview=lambda _: pytest.fail("overview already complete"),
        read_ad_posts=read,
        now=NOW,
        start=D,
        end=D,
        progress=progress,
    )
    assert not session.exec(select(MaterialPostLink)).all()
    assert needs(session, sync_seed)
    assert material_links.wait_for_dependencies(session, job, now=NOW)


def test_two_types_with_same_id_are_independent_owner_targets(session, sync_seed):
    first = D - timedelta(days=1)
    video = fact(session, sync_seed, "same-id", "5", "0", day=first)
    carousel = fact(session, sync_seed, "same-id", "5", "0")
    carousel.main_material_type = CAROUSELS[0]
    video.main_material_name = carousel.main_material_name = "same-name"
    owners = {"carousel-ad": CAROUSELS[0], "video-ad": "VIDEO_SPARK_ADS"}
    for ad in owners:
        session.add(
            AdMaterialReference(
                tenant_id=sync_seed.tenant_id,
                advertiser_id="acc-1",
                ad_remote_id=ad,
                platform_material_id=ad,
                material_type="IMAGE" if ad == "carousel-ad" else "VIDEO",
                name="same-name",
                complete=True,
                published_version=1,
            )
        )
    session.flush()
    missing = material_links._unlinked(
        session, sync_seed.tenant_id, "acc-1", start=first, end=D
    )
    assert len(missing) == 2
    assert {entry[1] for entry in missing.values()} == {"VIDEO_SPARK_ADS", CAROUSELS[0]}
    calls, progress = [], {"overview_complete": True}

    def read(ads, _start, _end, page):
        calls.append(ads)
        rows = tuple(
            {
                "dimensions": {
                    "main_material_id": "same-id",
                    "main_material_type": owners[ad],
                }
            }
            for ad in ads
        )
        return RawReportPage(rows, page, 1, len(rows))

    for _ in range(12):
        before = len(calls)
        done = material_links.refresh_material_links(
            session,
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            read_overview=lambda _: pytest.fail("already complete"),
            read_ad_posts=read,
            now=NOW,
            start=first,
            end=D,
            progress=progress,
        )
        assert len(calls) - before <= 1
        if done:
            break
    assert done
    assert {
        (link.ad_remote_id, link.main_material_type)
        for link in session.exec(select(MaterialPostLink)).all()
    } == set(owners.items())
    # 轮播归属已完成，视频未获 VID 仍须等待。
    assert material_links.missing_material_link_ids(
        session,
        tenant_id=sync_seed.tenant_id,
        advertiser_id="acc-1",
        start=first,
        end=D,
    ) == {"same-id"}


def test_cached_membership_preserves_provider_type():
    from app.modules.reporting.link_queries import QueryMembershipReader

    row = {
        "dimensions": {"main_material_id": "post", "main_material_type": CAROUSELS[0]}
    }
    cache, calls = {}, []

    def read(*args):
        calls.append(args)
        return RawReportPage((row,), 1, 1, 1)

    reader = QueryMembershipReader(
        read, cache, targets=[material_identity_key("post", CAROUSELS[0])], now=NOW
    )
    assert reader(("ad",), D, D, 1).rows == (row,)
    assert reader(("ad",), D, D, 1).rows == (row,)
    assert len(calls) == 1
