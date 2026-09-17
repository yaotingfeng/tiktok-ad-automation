"""首次共享只查询冻结源MID；UNKNOWN不使用空筛选作为重发许可。"""

import json
from datetime import timedelta

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_cover_throughput import (
    single_page_checkpoints as single_page_checkpoints,
)
from tests.modules.materials.test_covers import job_state, run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_first_share_uses_complete_scoped_inventory_not_entire_account(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 2, 1)[0]
    wire[1].extend([{"list": [image(0), image(1)]}, page([]), {"failed_infos": {}}])
    run(source_env, redis_client, identity)
    searches = [call for call in wire[0] if "image/ad/search" in call[1]]
    assert len(searches) == 1
    assert json.loads(dict(searches[0][2]["fields"])["filtering"]) == {
        "material_ids": ["900000", "900001"]
    }
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    wire[1].append(page([]))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    wire[1].append(page([image(0, target_id=True), image(1, target_id=True)]))
    drive(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_first_share_reuses_matches_and_only_sends_missing_member(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 2, 1)
    wire[1].extend(
        [
            {"list": [image(0), image(1)]},
            page([image(0, target_id=True)]),
            {"failed_infos": {}},
        ]
    )
    run(source_env, redis_client, identities[0])
    query = next(call for call in wire[0] if "image/ad/search" in call[1])
    assert "filtering" in dict(query[2]["fields"])
    posts = [json.loads(call[2]["body"]) for call in wire[0] if call[0] == "POST"]
    assert len(posts) == 1 and posts[0]["material_ids"] == ["900001"]
    assert job_state(identities[0]).status == "READY"
    wire[1].append(page([image(1, target_id=True)]))
    drive(source_env, redis_client, identities[1], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)


def test_reused_cover_with_no_batch_post_never_becomes_first_share(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    wire[1].extend([{"list": [image(0)]}, page([image(0, target_id=True)])])
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "READY"
    with Session(engine) as db, db.begin():
        job = db.get(MaterialCoverJob, identity)
        batch = db.get(MaterialCoverShareBatch, job.share_batch_id)
        assert batch.armed_at is None
        job.updated_at = covers._now() - timedelta(hours=2)
        covers.request_cover_reconciliation(
            db, context=source_env["context"], job_id=identity
        )
    wire[1].extend([page([]), page([])])
    drive(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).error_code == "cover_result_unknown"
    assert not [call for call in wire[0] if call[0] == "POST"]


@pytest.mark.usefixtures("single_page_checkpoints")
def test_legacy_complete_scan_still_probes_each_target_before_first_share(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 1, 2)
    wire[1].extend([{"list": [image(0)]}, page([])])
    run(source_env, redis_client, identities[0])
    with Session(engine) as db, db.begin():
        batch = db.get(MaterialCoverShareBatch, job_state(identities[0]).share_batch_id)
        batch.scan_state = {
            **batch.scan_state,
            "target-1": {"done": True, "found": {}, "seen": [], "page": 1},
        }
    wire[1].extend([page([image(0, target_id=True)]), {"failed_infos": {}}])
    run(source_env, redis_client, identities[0])
    assert job_state(identities[1]).status == "READY"
    posts = [json.loads(call[2]["body"]) for call in wire[0] if call[0] == "POST"]
    assert len(posts) == 1 and posts[0]["shared_advertiser_ids"] == ["target-0"]
