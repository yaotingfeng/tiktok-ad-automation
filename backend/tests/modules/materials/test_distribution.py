"""Submission and workers with real PG/Redis, official SDK and S3 wire doubles."""

from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials.distribution import ensure_target_asset, run_distribution
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialUploadAttempt,
)
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import (
    info,
    seed_operation,
)
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import (
    run as run_source,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def queue(env, account):
    with Session(engine) as session, session.begin():
        return ensure_target_asset(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id=account,
            task_key=f"build:{uuid4()}",
        )


def run(env, redis_client, dist_id, *, kind="verify", **kwargs):
    run_distribution(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        distribution_id=dist_id,
        kind=kind,
        **kwargs,
    )


def state(dist_id):
    with Session(engine) as session:
        dist = session.get(MaterialDistribution, dist_id)
        op = session.get(MaterialAssetOperation, dist.operation_id)
        mapping = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == dist.tenant_id,
                AccountMaterial.material_id == dist.material_id,
                AccountMaterial.advertiser_id == dist.advertiser_id,
            )
        ).first()
        session.expunge_all()
        return dist, op, mapping


def test_two_submissions_share_durable_distribution_not_message_id(source_env, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    first, second = queue(source_env, account), queue(source_env, account)
    assert first.state == second.state == "queued" and first.task_id == second.task_id
    with Session(engine) as session:
        messages = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id
            )
        ).all()
        assert len(messages) == 1 and messages[0].id != first.task_id
    assert wire[0] == []


def test_target_upload_and_readback_use_actual_target_vid_without_changing_source(
    source_env, redis_client, wire, original_s3
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        source = asset(session, source_env, "actual-account")
        source.image_id = "source-cover-id"
        source.cover_url = "https://source.example.invalid/image"
        source_id = source.id
    prepared = queue(source_env, account)
    wire[1].append([{"video_id": "received-target", "material_id": "received-mid"}])
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    assert state(prepared.task_id)[0].status == "verifying"
    assert state(prepared.task_id)[2] is None
    wire[1].append(info(vid="actual-target"))
    run(source_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and op.status == "succeeded"
    assert mapping.advertiser_id == account and mapping.video_id == "actual-target"
    assert mapping.image_id is None and mapping.cover_url is None
    assert [dict(call[2]["fields"])["advertiser_id"] for call in wire[0]] == [
        account,
        account,
    ]
    with Session(engine) as session:
        assert session.get(AccountMaterial, source_id).video_id == "vid-actual-account"
        assert (
            session.exec(
                select(MaterialUploadAttempt).where(
                    MaterialUploadAttempt.material_id == source_env["material_id"]
                )
            ).all()
            == []
        )
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    assert len(wire[0]) == 2


def test_distribution_waits_on_existing_source_operation_without_second_sender(
    source_env, redis_client, wire
):
    op_id = seed_operation(source_env)
    prepared = queue(source_env, "actual-account")
    assert state(prepared.task_id)[1].id == op_id
    run(source_env, redis_client, prepared.task_id)
    assert wire[0] == []
    wire[1].append(info())
    run_source(source_env, redis_client, operation_id=op_id)
    run(source_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[0].status == "ready" and len(wire[0]) == 1


def test_expired_target_is_read_again_without_original_upload(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=1000)
    prepared = queue(source_env, account)
    wire[1].append(info(vid="current-target"))
    run(source_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[2].video_id == "current-target"
    assert [call[0] for call in wire[0]] == ["GET"]


def test_unknown_upload_never_switches_path_or_reuploads(
    source_env, redis_client, wire, original_s3
):
    from urllib3.exceptions import ReadTimeoutError

    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    prepared = queue(source_env, account)
    wire[1].append(
        ReadTimeoutError(None, "https://offline.invalid", "offline-token-secret")
    )
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    assert state(prepared.task_id)[0].status == "result_unknown"
    run(source_env, redis_client, prepared.task_id, kind="prepare", s3=original_s3[0])
    wire[1].append(
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 0}}
    )
    run(source_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[1].status == "result_unknown"
    assert [call[0] for call in wire[0]] == ["POST", "GET"]


def test_unknown_share_does_not_assume_source_mid_maps_to_target(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        source = asset(session, source_env, "actual-account")
        op = MaterialAssetOperation(
            tenant_id=source_env["context"].tenant_id,
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id=account,
            path="share_source",
            status="result_unknown",
            request_digest="a" * 64,
            remote_response={"source_mid": source.mid},
        )
        session.add(op)
        session.flush()
    prepared = queue(source_env, account)
    wire[1].append(
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 0}}
    )
    run(source_env, redis_client, prepared.task_id)
    dist, op, _ = state(prepared.task_id)
    assert op.path == "share_source" and dist.status == "result_unknown"
    assert len(wire[0]) == 1 and wire[0][0][0] == "GET"
    assert "filtering" not in dict(wire[0][0][2]["fields"])


def test_revocation_before_target_write_blocks_it(source_env, redis_client, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    prepared = queue(source_env, account)
    with Session(engine) as session, session.begin():
        session.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                account,
                source_env["connection_id"],
            ),
        ).can_build = False
    run(source_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "blocked" and wire[0] == []


def test_unsent_share_without_capability_blocks_after_source_revocation(
    source_env, redis_client, wire, original_s3
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, "actual-account")
        session.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                "actual-account",
                source_env["connection_id"],
            ),
        ).authorized = False
        session.add(
            MaterialAssetOperation(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                material_id=source_env["material_id"],
                advertiser_id=account,
                path="share_source",
                status="pending",
                request_digest="a" * 64,
            )
        )
    dist_id = queue(source_env, account).task_id
    wire[1].append([{"video_id": "target-receipt"}])
    run(source_env, redis_client, dist_id, kind="prepare", s3=original_s3[0])
    dist, op, _ = state(dist_id)
    assert op.path == "share_source"
    assert dist.status == "blocked" and op.status == "failed"
    assert op.remote_response["definite_no_effect"] is True
    assert len(wire[0]) == 0


def test_definitely_rejected_share_uses_new_upload_operation_preserving_failure(
    source_env, redis_client, wire, original_s3
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    dist_id = queue(source_env, account).task_id
    old_id = state(dist_id)[1].id
    with Session(engine) as session, session.begin():
        old = session.get(MaterialAssetOperation, old_id)
        old.path, old.status = "share_source", "failed"
        old.remote_response = {
            "definite_no_effect": True,
            "error_code": "share_unsupported",
        }
    wire[1].append([{"video_id": "new-target-receipt"}])
    run(source_env, redis_client, dist_id, kind="prepare", s3=original_s3[0])
    assert state(dist_id)[1].id != old_id and state(dist_id)[1].status == "verifying"
    with Session(engine) as session:
        assert session.get(MaterialAssetOperation, old_id).status == "failed"
    assert len(wire[0]) == 1


def test_unproven_failed_share_cannot_start_upload_on_new_submission(source_env, wire):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        session.add(
            MaterialAssetOperation(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                material_id=source_env["material_id"],
                advertiser_id=account,
                path="share_source",
                status="failed",
                request_digest="a" * 64,
                remote_response={"error_code": "timeout"},
            )
        )
    result = queue(source_env, account)
    assert (
        result.state == "blocked" and result.reason_code == "material_share_unconfirmed"
    )
    assert wire[0] == []


def test_target_permission_revoked_during_read_does_not_publish_asset(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=1000)
    dist_id = queue(source_env, account).task_id

    def response():
        with Session(engine) as session, session.begin():
            session.get(
                BCAccountAccess,
                (
                    source_env["context"].tenant_id,
                    source_env["bc_id"],
                    account,
                    source_env["connection_id"],
                ),
            ).can_build = False
        return info(vid="must-not-publish")

    wire[1].append(response)
    run(source_env, redis_client, dist_id)
    assert state(dist_id)[2].video_id != "must-not-publish"
    assert state(dist_id)[0].status != "ready"


def test_read_recovery_of_definite_failure_queues_write_in_bounded_upload_handler(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
    dist_id = queue(source_env, account).task_id
    old_id = state(dist_id)[1].id
    with Session(engine) as session, session.begin():
        old = session.get(MaterialAssetOperation, old_id)
        old.path, old.status = "share_source", "failed"
        old.remote_response = {"definite_no_effect": True}
    run(source_env, redis_client, dist_id, kind="verify")
    current = state(dist_id)[1]
    assert current.id != old_id and current.status == "pending"
    with Session(engine) as session:
        messages = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == source_env["context"].tenant_id,
                PendingDispatch.task_name == "materials.prepare_target",
            )
        ).all()
        assert any(row.payload["operation_id"] == str(current.id) for row in messages)
    assert wire[0] == []


def test_source_verification_survives_original_loss_and_target_observer_finishes(
    source_env, redis_client, wire
):
    from app.modules.materials.models import MaterialFile

    op_id = seed_operation(source_env)
    with Session(engine) as session, session.begin():
        session.get(
            MaterialFile, source_env["material_id"]
        ).storage_state = "unavailable"
    dist_id = queue(source_env, "actual-account").task_id
    wire[1].append(info())
    run_source(source_env, redis_client, operation_id=op_id)
    run(source_env, redis_client, dist_id)
    assert state(dist_id)[0].status == "ready" and len(wire[0]) == 1
