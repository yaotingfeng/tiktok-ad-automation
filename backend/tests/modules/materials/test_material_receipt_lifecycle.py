"""成功回执与持久映射：只在真实失效或明确核查时读取平台。"""

from datetime import timedelta

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from tests.modules.materials.test_covers import (
    image_info,
    job_state,
    successful_upload,
)
from tests.modules.materials.test_covers import (
    queue as queue_cover,
)
from tests.modules.materials.test_covers import (
    run as run_cover,
)
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, read, target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import run as run_source
from tests.modules.materials.test_source_uploads import seed_operation, snapshot
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


@pytest.mark.parametrize("mid", [None, "actual-target-mid"])
def test_relay_receipt_publishes_without_readback_and_duplicate_send(
    remote_env, redis_client, wire, mid
):
    prepared = queue(remote_env, remote_env["target"])
    receipt = {"video_id": "actual-target"}
    if mid:
        receipt["material_id"] = mid
    wire[1].extend([info(), [receipt]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, operation, mapping = state(prepared.task_id)
    assert dist.status == "ready" and operation.status == "succeeded"
    assert mapping.video_id == "actual-target" and mapping.mid == mid
    assert operation.remote_response["confirmation_source"] == "upload_receipt"
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    run(remote_env, redis_client, prepared.task_id)
    assert [call[0] for call in wire[0]] == ["GET", "POST"]


def test_file_upload_receipt_publishes_without_readback(
    source_env, redis_client, wire, original_s3
):
    operation_id = seed_operation(source_env, status="pending", evidence={})
    wire[1].append([{"video_id": "actual-upload", "material_id": "actual-mid"}])
    run_source(
        source_env,
        redis_client,
        operation_id=operation_id,
        kind="upload",
        s3=original_s3[0],
    )
    operation, attempt, mapping = snapshot(source_env, operation_id)
    assert operation.status == "succeeded" and attempt.status == "available"
    assert mapping.video_id == "actual-upload" and mapping.mid == "actual-mid"
    assert operation.remote_response["confirmation_source"] == "upload_receipt"
    run_source(source_env, redis_client, operation_id=operation_id)
    assert [call[0] for call in wire[0]] == ["POST"]


def test_old_video_mapping_stays_ready_but_revoked_access_still_blocks(
    source_env, wire
):
    with Session(engine) as db, db.begin():
        account = target(db, source_env)
        asset(db, source_env, account, seconds_old=86400)
    assert read(source_env, account).state == "ready"
    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                account,
                source_env["connection_id"],
            ),
        ).authorized = False
    assert read(source_env, account).state == "blocked"
    assert not wire[0]


def test_old_cover_mapping_is_ready_without_automatic_reads(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity))
    run_cover(source_env, redis_client, identity, read=True)
    with Session(engine) as db, db.begin():
        job = db.get(MaterialCoverJob, identity)
        job.updated_at = covers._now() - timedelta(days=1)
        db.get(AccountMaterial, job.asset_id).verified_at = job.updated_at
    before = len(wire[0])
    assert queue_cover(source_env).state == "ready"
    with Session(engine) as db:
        assert (
            covers.get_cover_status(
                db, context=source_env["context"], job_id=identity
            ).state
            == "ready"
        )
    assert job_state(identity).dispatch_id is None and len(wire[0]) == before
