"""已有成功回执的素材不被遗留只读消息降级或重复核查。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
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
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("source_owned", [False, True])
def test_successful_original_receipt_ignores_redundant_read_delivery(
    source_env, redis_client, wire, original_s3, source_owned
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    prepared = queue(source_env, account)
    wire[1].append([{"video_id": "target-actual", "material_id": "mid-actual"}])
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    run(source_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and op.status == "succeeded"
    revision = op.remote_response["revision"]
    with Session(engine) as session, session.begin():
        asset = session.get(AccountMaterial, mapping.id)
        confirmed_at = datetime.now(UTC) - timedelta(hours=1)
        asset.verified_at = confirmed_at
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
    assert actual_dist.status == "ready"
    assert actual_mapping.video_id == "target-actual"
    assert actual_mapping.verified_at == confirmed_at
    assert len(wire[0]) == 1
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
    assert len(wire[0]) == 1
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


@pytest.mark.parametrize("changed", ["unavailable", "conflict", "revoked"])
def test_receipt_reuse_does_not_ignore_negative_facts(
    source_env, redis_client, wire, original_s3, changed
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    prepared = queue(source_env, account)
    wire[1].append([{"video_id": "target-actual", "material_id": "mid-actual"}])
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready"
    with Session(engine) as session, session.begin():
        if changed == "unavailable":
            session.get(AccountMaterial, mapping.id).status = "unavailable"
        elif changed == "conflict":
            operation = session.get(MaterialAssetOperation, op.id)
            operation.remote_response = {
                **operation.remote_response,
                "conflicting_video_id": "another-target",
            }
        else:
            session.get(
                BCAccountAccess,
                (op.tenant_id, op.bc_id, account, mapping.connection_id),
            ).authorized = False
    wire[1].append({"list": []})
    run(source_env, redis_client, dist.id, read_only=True)
    assert state(dist.id)[0].status != "ready"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
