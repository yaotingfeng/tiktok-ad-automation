"""非 Spark 主素材是库 MID；只能用同账户官方 MID/VID 成对证据补映射。"""

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncJob
from tests.modules.reporting.test_backlog_control import request
from tests.modules.reporting.test_material_link_resume import step
from tests.modules.reporting.test_material_query import D, material_row, publish


def seed_identity(factory, seed, *, kind="VIDEO_NON_SPARK_ADS", count=1):
    with factory() as session:
        rows = [material_row(str(i), "1", "same-name.mp4") for i in range(count)]
        for row in rows:
            row["dimensions"]["main_material_type"] = kind
        publish(session, seed, "acc-1", rows)
        for i in range(count):
            session.add(
                MaterialPostLink(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    main_material_id=str(i),
                    ad_remote_id="ad",
                    main_material_type=kind,
                    observed_at=datetime.now(UTC),
                )
            )
        job_id = request(session, seed, D)
        session.commit()
        return job_id


class Reader:
    def __init__(self, *, empty=False):
        self.queries = []
        self.empty = empty

    def material_videos(self, *, advertiser_id, material_ids):
        assert advertiser_id == "acc-1"
        self.queries.append(material_ids)
        rows = (
            ()
            if self.empty
            else tuple(
                {"material_id": mid, "video_id": "vid-" + mid} for mid in material_ids
            )
        )
        return RawReportPage(rows, 1, 1 if rows else 0, len(rows))

    def material_ad_links(self, **kwargs):
        return RawReportPage((), 1, 0, 0)


def test_non_spark_mid_uses_exact_remote_identity_and_finishes(committed_sync_seed):
    seed, factory = committed_sync_seed
    job_id = seed_identity(factory, seed)
    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.queries == [("0",)]
    assert step(factory, job_id, reader) == "COMPLETE"
    with factory() as session:
        assert (
            session.exec(select(MaterialPostLink)).one().platform_material_id == "vid-0"
        )
        assert session.get(ReportSyncJob, job_id).request_count == 1


def test_non_spark_batches_resume_without_rereading_resolved_ids(committed_sync_seed):
    seed, factory = committed_sync_seed
    job_id = seed_identity(factory, seed, count=51)
    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "COMPLETE"
    assert list(map(len, reader.queries)) == [20, 20, 11]
    assert len({mid for batch in reader.queries for mid in batch}) == 51


@pytest.mark.parametrize("kind", ["VIDEO_SPARK_ADS", "CAROUSEL_NON_SPARK_ADS"])
def test_other_namespaces_are_not_searched_as_video_mid(committed_sync_seed, kind):
    seed, factory = committed_sync_seed
    job_id = seed_identity(factory, seed, kind=kind)
    reader = Reader()
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.queries == []
    with factory() as session:
        assert session.exec(select(MaterialPostLink)).one().platform_material_id is None


def test_missing_asset_stays_missing_and_is_not_spun_on_every_step(committed_sync_seed):
    seed, factory = committed_sync_seed
    job_id = seed_identity(factory, seed)
    reader = Reader(empty=True)
    assert step(factory, job_id, reader) == "WAITING"
    assert step(factory, job_id, reader) == "WAITING"
    assert reader.queries == [("0",)]
    with factory() as session:
        assert session.exec(select(MaterialPostLink)).one().platform_material_id is None
        job = session.get(ReportSyncJob, job_id)
        assert job.status == "WAITING" and job.due_at > datetime.now(UTC)
