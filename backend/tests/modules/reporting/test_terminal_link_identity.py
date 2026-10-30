"""最后一页/最后一个归属查询产生的新身份工作必须立即续步，不能误入依赖退避。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.store_models import (
    MaterialPostLink,
    MaterialReportDay,
    ReportSyncJob,
)
from tests.modules.reporting.test_material_link_resume import step
from tests.modules.reporting.test_material_links import overview
from tests.modules.reporting.test_spark_content_identity import Reader, prepare


@pytest.mark.parametrize("kind", ["VIDEO_SPARK_ADS", "VIDEO_NON_SPARK_ADS"])
@pytest.mark.parametrize("source", ["overview", "owner"])
@pytest.mark.parametrize("available", [True, False])
def test_last_owner_gets_identity_before_dependency_wait(
    committed_sync_seed, kind, source, available
):
    seed, factory = committed_sync_seed
    with factory() as session:
        job, link = prepare(session, seed)
        job_id = job.id
        session.delete(link)
        fact = session.exec(select(MaterialReportDay)).one()
        fact.main_material_type = kind
        usage = session.exec(select(AdMaterialReference)).one()
        usage.name = "file.mp4"
        if source == "owner":
            job.progress = {"overview_complete": True}
        session.commit()

    class TerminalReader(Reader):
        def material_ad_links(self, **kwargs):
            self.history.append(("overview", kwargs))
            page = overview(("ad", "post", None, "file.mp4"))
            page.rows[0]["dimensions"]["main_material_type"] = kind
            return page

        def material_ad_posts(self, **kwargs):
            self.history.append(("owner", kwargs))
            return RawReportPage(
                (
                    {
                        "dimensions": {
                            "main_material_id": "post",
                            "main_material_type": kind,
                        }
                    },
                ),
                1,
                1,
                1,
            )

        def material_videos(self, **kwargs):
            self.history.append(("mid", kwargs))
            return RawReportPage(
                ({"material_id": "post", "video_id": "vid"},) if available else (),
                1,
                1 if available else 0,
                int(available),
            )

    reader = TerminalReader()
    if not available:
        reader.content = None
    for _ in range(6):
        before = len(reader.history)
        outcome = step(factory, job_id, reader)
        assert len(reader.history) - before <= 1
        with factory() as session:
            job = session.get(ReportSyncJob, job_id)
            if job.error_code is not None:
                assert not available
                assert job.error_code in {
                    "material_identity_unavailable",
                    "material_directory_pending",
                }
                assert any(op in {"post", "mid"} for op, _ in reader.history)
                assert job.due_at > datetime.now(UTC) + timedelta(minutes=9)
                assert outcome == "WAITING"
                break
            assert job.due_at < datetime.now(UTC) + timedelta(seconds=5)
        if outcome == "COMPLETE":
            break
    else:
        raise AssertionError("最后一个已知归属必须立即完成身份续步")
    with factory() as session:
        assert session.exec(select(MaterialPostLink)).one().platform_material_id == (
            "vid" if available else None
        )
