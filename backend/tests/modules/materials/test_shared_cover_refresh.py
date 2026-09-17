"""已完成共享的目标封面刷新应批读真实ID，不重跑共享库存扫描。"""

from datetime import timedelta

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from app.modules.materials.models import AccountMaterial
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import job_state, run, video_info
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("missing_image", [None, 1])
@pytest.mark.parametrize("batch_status", ["READY", "UNKNOWN"])
def test_shared_known_images_refresh_video_and_image_in_two_reads_without_rescan(
    source_env, redis_client, wire, missing_image, batch_status
):
    identities = matrix(source_env, 3, 1)
    with Session(engine) as db, db.begin():
        for index, identity in enumerate(identities):
            current = db.get(MaterialCoverJob, identity)
            current.video_id = f"target-video-{index}"
            db.get(AccountMaterial, current.asset_id).video_id = current.video_id
    wire[1].extend(
        [
            {"list": [image(i) for i in range(3)]},
            page([]),
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    wire[1].append(page([image(i, target_id=True) for i in range(3)]))
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    with Session(engine) as db, db.begin():
        batch = db.get(MaterialCoverShareBatch, job_state(identities[0]).share_batch_id)
        # 历史刷新失败不抹掉已核实的目标ID，也不能强迫重新搜索已知图片。
        batch.status = batch_status
        original_batch = batch.model_dump()
        videos = []
        for identity in identities:
            job = db.get(MaterialCoverJob, identity)
            job.updated_at = covers._now() - timedelta(hours=1)
            db.get(AccountMaterial, job.asset_id).verified_at = job.updated_at
            videos.append({**video_info()["list"][0], "video_id": job.video_id})
            covers.request_cover_reconciliation(
                db, context=source_env["context"], job_id=identity
            )
    before = len(wire[0])
    wire[1].extend(
        [
            {"list": videos},
            {
                "list": [
                    image(i, target_id=True) for i in range(3) if i != missing_image
                ]
            },
        ]
    )
    run(source_env, redis_client, identities[0], read=True)
    assert [job_state(identity).status for identity in identities] == [
        "UNKNOWN" if i == missing_image else "READY" for i in range(3)
    ]
    assert len(wire[0]) - before == 2
    assert all(call[0] == "GET" and "/info/" in call[1] for call in wire[0][before:])
    with Session(engine) as db:
        assert (
            db.get(MaterialCoverShareBatch, original_batch["id"]).model_dump()
            == original_batch
        )
        assert all(
            db.get(AccountMaterial, job_state(identity).asset_id).verified_at
            > covers._now() - timedelta(seconds=30)
            for identity in identities
        )
    assert sum(call[0] == "POST" for call in wire[0]) == 1


@pytest.mark.parametrize("missing", ["identity", "member"])
def test_partial_shared_batch_is_not_claimed_as_complete_bulk_refresh(
    source_env, redis_client, wire, missing
):
    identities = matrix(source_env, 3, 1)
    wire[1].extend(
        [
            {"list": [image(i) for i in range(3)]},
            page([]),
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    wire[1].append(page([image(i, target_id=True) for i in range(3)]))
    drive(source_env, redis_client, identities[0], read=True)
    with Session(engine) as db, db.begin():
        first = db.get(MaterialCoverJob, identities[0])
        batch = db.get(MaterialCoverShareBatch, first.share_batch_id)
        batch.status = "UNKNOWN"
        for identity in identities:
            current = db.get(MaterialCoverJob, identity)
            current.updated_at = covers._now() - timedelta(hours=1)
            covers.request_cover_reconciliation(
                db, context=source_env["context"], job_id=identity
            )
        last = db.get(MaterialCoverJob, identities[-1])
        if missing == "identity":
            last.known_image_id = None
        else:
            last.share_batch_id = None
        db.flush()
        assert batch.id not in db.exec(covers._completed_cover_batches(first)).all()
        selected = {
            row.id for row in db.exec(covers._known_cover_candidates(first)).all()
        }
        assert identities[1] not in selected
        assert db.get(MaterialCoverJob, identities[1]).dispatch_id is not None
