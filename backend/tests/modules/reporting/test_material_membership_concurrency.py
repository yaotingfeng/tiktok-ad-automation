"""同VID的并发源发布必须串行合并成员，不能丢失另一事务的广告位置。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import text

from app.modules.ads.models import AdMaterialReference


def test_concurrent_same_video_publish_keeps_both_ad_positions(committed_sync_seed):
    seed, factory = committed_sync_seed
    first_written = Event()
    second_started = Event()
    release_first = Event()
    second_finished = Event()

    def write(ad, first):
        with factory() as session:
            if not first:
                assert first_written.wait(5)
                second_started.set()
            session.add(
                AdMaterialReference(
                    tenant_id=seed.tenant_id,
                    advertiser_id="acc-1",
                    ad_remote_id=ad,
                    platform_material_id="concurrent-video",
                    ad_material_id=f"material-{ad}",
                    material_type="VIDEO",
                    complete=True,
                    published_version=1,
                )
            )
            session.flush()
            if first:
                first_written.set()
                assert release_first.wait(10)
            session.commit()
            if not first:
                second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first", True)
        second = pool.submit(write, "second", False)
        try:
            assert first_written.wait(5)
            assert second_started.wait(5)
            # 第二事务已进入写入，第一事务的VID锁还未释放。
            assert not second_finished.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=15)
        second.result(timeout=15)
    with factory() as session:
        version = (
            session.connection()
            .execute(
                text("""SELECT v.refs,v.material_uses,h.use_count
            FROM material_account_member_current h JOIN report_evidence_version v ON v.id=h.version_id
            WHERE h.tenant_id=:t AND h.advertiser_id='acc-1' AND h.platform_material_id='concurrent-video'"""),
                {"t": seed.tenant_id},
            )
            .one()
        )
        assert version.use_count == 2
        assert [r["remote_id"] for r in version.refs] == ["first", "second"]
        assert [u["ad_material_id"] for u in version.material_uses] == [
            "material-first",
            "material-second",
        ]
