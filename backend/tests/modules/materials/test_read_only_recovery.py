"""Strict manual reads preserve receipt and cannot create a new upload authority."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import queue_distribution
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialUploadAttempt,
)
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_source_uploads import info
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("source_owned", [False, True])
def test_successful_original_operation_can_only_revalidate_known_vid(
    source_env, redis_client, wire, original_s3, source_owned
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    prepared = queue(source_env, account)
    wire[1].append([{"video_id": "target-actual", "material_id": "mid-actual"}])
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    wire[1].append(info(vid="target-actual"))
    run(source_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and op.status == "succeeded"
    revision = op.remote_response["revision"]
    with Session(engine) as session, session.begin():
        asset = session.get(AccountMaterial, mapping.id)
        asset.verified_at = datetime.now(UTC) - timedelta(hours=1)
        if source_owned:
            session.add(
                MaterialUploadAttempt(
                    tenant_id=op.tenant_id,
                    bc_id=op.bc_id,
                    material_id=op.material_id,
                    advertiser_id=op.advertiser_id,
                    connection_id=asset.connection_id,
                    operation_id=op.id,
                    status="succeeded",
                    request_digest=op.request_digest,
                )
            )
        session.add(asset)
        queue_distribution(
            session,
            session.get(MaterialDistribution, dist.id),
            session.get(MaterialAssetOperation, op.id),
            kind="verify",
            observe=True,
            read_only=True,
        )
    wire[1].append(info(vid="target-actual"))
    run(
        source_env,
        redis_client,
        dist.id,
        kind="verify",
        read_only=True,
        operation_id=op.id,
        revision=revision,
    )
    actual_dist, actual_op, actual_mapping = state(dist.id)
    assert actual_dist.operation_id == op.id and actual_op.status == "succeeded"
    assert actual_mapping.verified_at > datetime.now(UTC) - timedelta(seconds=30)
    assert len(wire[0]) == 3
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    # A stale duplicate cannot use its former revision to refresh again.
    run(
        source_env,
        redis_client,
        dist.id,
        kind="verify",
        read_only=True,
        operation_id=op.id,
        revision=revision,
    )
    assert len(wire[0]) == 3
    with Session(engine) as session:
        pending = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.task_name == "materials.verify_target"
            )
        ).all()
        assert any(p.payload.get("read_only") is True for p in pending)

    from app.modules.materials.distribution import repair_material_dispatches

    with Session(engine) as session, session.begin():
        for dispatch in session.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == op.tenant_id)
        ).all():
            if dispatch.payload.get("read_only"):
                dispatch.published_at = datetime.now(UTC) - timedelta(minutes=5)
                dispatch.available_at = datetime.now(UTC) - timedelta(minutes=5)
                session.add(dispatch)
        session.flush()
        assert repair_material_dispatches(session) == 0
