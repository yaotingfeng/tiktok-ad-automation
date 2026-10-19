"""旧 Spark 桥接缺失时，以平台内容摘要及大小匹配唯一原视频。"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.ads.models import AdObject
from app.modules.reporting.store_models import MaterialPostLink
from tests.modules.reporting.test_bridge_dependencies import seed_bridge

DIGEST = "a" * 32


def prepare(session, seed):
    job, link = seed_bridge(session, seed)
    ad = session.get(AdObject, (seed.tenant_id, "acc-1", "ad", "ad"))
    ad.configuration = {
        "creative_list": [
            {
                "creative_info": {
                    "identity_type": "BC_AUTH_TT",
                    "identity_id": "identity",
                    "identity_authorized_bc_id": "bc-a",
                    "video_info": {"video_id": vid},
                }
            }
            for vid in ("vid", "other")
        ]
    }
    session.flush()
    return job, link


class Reader:
    def __init__(self):
        self.history = []
        self.content = {"signature": DIGEST, "size": 123}
        self.assets = [
            {"video_id": "vid", **self.content},
            {"video_id": "other", "signature": "b" * 32, "size": 123},
        ]

    def material_video_content(self, **kwargs):
        self.history.append(("assets", kwargs))
        return self.assets

    def spark_video_content(self, **kwargs):
        self.history.append(("post", kwargs))
        return self.content


def resolve(session, job, reader):
    from app.modules.reporting.spark_identity import resolve_spark_videos

    return resolve_spark_videos(session, job, reader)


def test_exact_content_resolves_without_overview_or_name(session, sync_seed):
    job, link = prepare(session, sync_seed)
    reader = Reader()
    assert resolve(session, job, reader) is True
    assert link.platform_material_id is None
    assert resolve(session, job, reader) is True
    session.refresh(link)
    assert link.platform_material_id == "vid" and link.ad_material_id is None
    assert [op for op, _ in reader.history] == ["assets", "post"]
    assert reader.history[1][1]["item_id"] == "post"
    assert resolve(session, job, reader) is False


@pytest.mark.parametrize(
    "case", ["different_hash", "different_size", "ambiguous", "missing"]
)
def test_unproven_content_keeps_real_gap(session, sync_seed, case):
    job, link = prepare(session, sync_seed)
    reader = Reader()
    if case == "different_hash":
        reader.content = {"signature": "c" * 32, "size": 123}
    if case == "different_size":
        reader.content = {"signature": DIGEST, "size": 124}
    if case == "ambiguous":
        reader.assets[1] = {"video_id": "other", **reader.content}
    if case == "missing":
        reader.content = None
    assert resolve(session, job, reader)
    assert resolve(session, job, reader)
    assert not resolve(session, job, reader)
    session.refresh(link)
    assert link.platform_material_id is None
    assert len(reader.history) == 2


def test_same_owner_reuses_metadata_but_reads_each_post(session, sync_seed):
    job, link = prepare(session, sync_seed)
    from tests.modules.reporting.test_material_query import material_row, publish

    publish(
        session,
        sync_seed,
        "acc-1",
        [material_row("post", "1"), material_row("post2", "1")],
    )
    session.add(
        MaterialPostLink(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id="post2",
            ad_remote_id="ad",
            main_material_type="VIDEO_SPARK_ADS",
            observed_at=datetime.now(UTC),
        )
    )
    session.flush()
    reader = Reader()
    for _ in range(3):
        assert resolve(session, job, reader)
    assert not resolve(session, job, reader)
    assert [op for op, _ in reader.history] == ["assets", "post", "post"]
    session.flush()
    session.expire_all()
    assert all(
        x.platform_material_id == "vid"
        for x in session.exec(select(MaterialPostLink)).all()
    )


def test_ambiguous_identity_does_not_guess_account_identity(session, sync_seed):
    job, _ = prepare(session, sync_seed)
    ad = session.get(AdObject, (sync_seed.tenant_id, "acc-1", "ad", "ad"))
    config = deepcopy(ad.configuration)
    config["creative_list"][1]["creative_info"]["identity_id"] = "another"
    ad.configuration = config
    session.flush()
    reader = Reader()
    assert not resolve(session, job, reader)
    assert not reader.history


def test_provider_failure_keeps_same_post_retryable(session, sync_seed):
    job, link = prepare(session, sync_seed)
    reader = Reader()
    assert resolve(session, job, reader)

    def fail(**_kwargs):
        raise DomainError("tiktok_response_error", "retry")

    reader.spark_video_content = fail
    with pytest.raises(DomainError):
        resolve(session, job, reader)
    reader.spark_video_content = lambda **kwargs: reader.content
    assert resolve(session, job, reader)
    session.refresh(link)
    assert link.platform_material_id == "vid"


def test_metadata_and_post_resume_across_worker_transactions(committed_sync_seed):
    from app.modules.reporting.store_models import ReportSyncJob
    from tests.modules.reporting.test_material_link_resume import step

    seed, factory = committed_sync_seed
    with factory() as session:
        job, _ = prepare(session, seed)
        job_id = job.id
        session.commit()
    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "COMPLETE"
    with factory() as session:
        assert session.get(ReportSyncJob, job_id).request_count == 2
        assert (
            session.exec(select(MaterialPostLink)).one().platform_material_id == "vid"
        )
    assert [op for op, _ in reader.history] == ["assets", "post"]


def test_changed_owner_candidates_invalidate_cached_metadata(session, sync_seed):
    job, link = prepare(session, sync_seed)
    reader = Reader()
    assert resolve(session, job, reader)
    ad = session.get(AdObject, (sync_seed.tenant_id, "acc-1", "ad", "ad"))
    config = deepcopy(ad.configuration)
    config["creative_list"] = [config["creative_list"][1]]
    ad.configuration = config
    session.flush()
    reader.assets = [reader.assets[1]]
    assert resolve(session, job, reader)
    assert [op for op, _ in reader.history] == ["assets", "assets"]
    assert resolve(session, job, reader)
    session.refresh(link)
    assert link.platform_material_id is None
