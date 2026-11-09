"""同一定位步骤复用未变化的缺口快照，避免二次聚合相同日报。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting import material_links
from app.modules.reporting.material_links import refresh_material_links
from app.modules.reporting.publish import AccountMeta, publish_material_day
from tests.modules.reporting.test_material_link_resume import seed_job
from tests.modules.reporting.test_material_query import D, material_row


def test_locate_step_reads_unlinked_candidates_once(committed_sync_seed):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    reads, calls = [], []
    now = datetime.now(UTC)
    with factory() as baseline:
        signature = material_links._scan_signature(
            baseline, seed.tenant_id, "acc-1", {"old-post"}, D, D, ()
        )
    progress = {
        "typed_link_search": True,
        "overview_complete": True,
        "scan_evidence_ids": ["old-post"],
        "scan_evidence_signature": signature,
        "scan_evidence_at": now.isoformat(),
    }

    def posts(ads, first, last, page):
        calls.append((ads, first, last, page))
        return RawReportPage(
            (
                {
                    "dimensions": {
                        "main_material_id": "other",
                        "main_material_type": "VIDEO_SPARK_ADS",
                    }
                },
            ),
            1,
            2,
            2,
        )

    def capture(_conn, _cursor, statement, *_args):
        if "SELECT d.main_material_id AS mid" in statement:
            reads.append(statement)

    with factory() as session:
        conn = session.connection()
        event.listen(conn, "before_cursor_execute", capture)
        try:
            assert not refresh_material_links(
                session,
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                read_overview=lambda _: (_ for _ in ()).throw(
                    AssertionError("overview already complete")
                ),
                read_ad_posts=posts,
                now=datetime.now(UTC),
                start=D,
                end=D,
                progress=progress,
            )
        finally:
            event.remove(conn, "before_cursor_execute", capture)
        session.rollback()
    assert len(calls) == 1
    assert progress["locate_post"]["query"]["page"] == 2
    assert len(reads) == 1, "尚未改变关联或日报事实，缺口候选不应再聚合第二遍"


def test_new_baseline_rereads_candidate_span_after_concurrent_fact_publish(
    committed_sync_seed, monkeypatch
):
    seed, factory = committed_sync_seed
    seed_job(factory, seed)
    next_day = D + timedelta(days=1)
    original = material_links._scan_signature
    published, calls = [], []

    def signature(*args, **kwargs):
        if not published:
            # 另一事务在入口缺口读取后、基线登记前补入同帖的新日事实。
            row = material_row("old-post", "5", "same.mp4")
            row["dimensions"]["stat_time_day"] = str(next_day)
            with factory() as writer:
                publish_material_day(
                    writer,
                    tenant_id=seed.tenant_id,
                    job_id=uuid4(),
                    accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
                    start=next_day,
                    end=next_day,
                    rows=[row],
                )
                writer.commit()
            published.append(True)
        return original(*args, **kwargs)

    def posts(_ads, first, last, _page):
        calls.append((first, last))
        return RawReportPage((), 1, 2, 2)

    monkeypatch.setattr(material_links, "_scan_signature", signature)
    progress = {"typed_link_search": True, "overview_complete": True}
    with factory() as session:
        assert not refresh_material_links(
            session,
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            read_overview=lambda _: None,
            read_ad_posts=posts,
            now=datetime.now(UTC),
            start=D,
            end=next_day,
            progress=progress,
        )
        session.rollback()
    assert published == [True]
    assert calls == [(D, next_day)], "新基线中的事实必须进入本次候选日期范围"
