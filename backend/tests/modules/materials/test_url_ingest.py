"""Generation-fenced source ingestion: real DB/Redis and official SDK transport."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select
from urllib3.exceptions import ReadTimeoutError

from app.core.config import settings
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.accounts.routing import freeze_route
from app.modules.materials.ingest_models import (
    IngestSession,
    IngestSessionFile,
    OriginalUse,
    SourceAccountLoad,
    TemporaryMaterialObject,
    record_milestone,
)
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    MaterialUploadAttempt,
    ObjectUpload,
)
from app.modules.materials.source_uploads import run_source_upload
from tests.modules.materials.test_source_uploads import (  # noqa: F401
    CONTENT,
    MD5,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)
from tests.modules.materials.test_source_uploads import (
    wire as wire,
)

URL = "https://owned.r2.example/test-video?signature=never-store-this-url"


@pytest.fixture
def url_env(source_env, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "MATERIAL_INGEST_ENABLED", True)
    monkeypatch.setitem(settings.__dict__, "MATERIAL_URL_MAX_UPLOAD_BYTES", 1024**3)
    with Session(engine) as db, db.begin():
        material = db.get(MaterialFile, source_env["material_id"])
        material.current_object_generation = 1
        material.storage_state = (
            "unavailable"  # New path cannot depend on legacy flags.
        )
        material.digest_verified_at = datetime.now(UTC)
        material.digest_source = "r2-stream-verifier"
        db.execute(delete(ObjectUpload).where(ObjectUpload.material_id == material.id))
        parent = IngestSession(
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            actor_id=source_env["context"].actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            frozen_route=freeze_route(
                db,
                context=source_env["context"],
                bc_id=source_env["bc_id"],
                connection_id=source_env["connection_id"],
            ).model_dump(mode="json"),
            expected_files=1,
            expected_bytes=len(CONTENT),
        )
        db.add(parent)
        db.flush()
        db.add(
            IngestSessionFile(
                tenant_id=material.tenant_id,
                bc_id=material.bc_id,
                session_id=parent.id,
                client_index=0,
                material_id=material.id,
                byte_size=material.byte_size,
                manifest_digest="a" * 64,
            )
        )
        obj = TemporaryMaterialObject(
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            material_id=material.id,
            generation=1,
            object_key=material.object_key,
            expected_bytes=len(CONTENT),
            actual_bytes=len(CONTENT),
            reserved_bytes=len(CONTENT),
            status="verified",
            sha256=sha256(CONTENT).hexdigest(),
            video_md5=MD5,
            digest_verified_at=datetime.now(UTC),
            digest_source="r2-stream-verifier",
            storage_provider="r2",
            storage_endpoint="https://owned.r2.example",
            storage_bucket="test-bucket",
        )
        db.add(obj)
        db.flush()
        for milestone in ("accepted", "uploaded"):
            record_milestone(
                db,
                tenant_id=parent.tenant_id,
                bc_id=parent.bc_id,
                session_id=parent.id,
                material_id=material.id,
                milestone=milestone,
            )
        result = {
            **source_env,
            "object_id": obj.id,
            "generation": 1,
            "session_id": parent.id,
            "object_key": material.object_key,
        }

    class SignedOnly:
        def generate_presigned_url(self, method, **kwargs):
            assert method == "get_object"
            assert kwargs["Params"]["Key"] == result["object_key"]
            return URL

    result["s3"] = SignedOnly()
    return result


def run(env, redis_client, *, kind="upload", operation_id=None, **kwargs):
    kwargs.setdefault("s3", env["s3"])
    run_source_upload(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        material_id=env["material_id"],
        object_id=env["object_id"],
        generation=env["generation"],
        kind=kind,
        operation_id=operation_id,
        **kwargs,
    )


def operation(env):
    with Session(engine) as db:
        row = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == env["material_id"]
            )
        ).one()
        db.expunge(row)
        return row


def info(**kwargs):
    return {
        "list": [
            {
                "video_id": "actual-source-vid",
                "material_id": "actual-source-mid",
                "signature": MD5,
                "displayable": True,
                "width": 1080,
                "height": 1920,
                "duration": 4.5,
                "size": len(CONTENT),
                "format": "mp4",
                **kwargs,
            }
        ]
    }


@pytest.fixture
def interrupted_publication(monkeypatch):
    from app.modules.materials import source_url_uploads

    original = source_url_uploads._finish

    def fail_upload_publication(*args, **kwargs):
        # 回执已落库但本地发布中断，恢复任务仍须能够严格核查原操作。
        if kwargs["kind"] == "upload":
            raise RuntimeError("synthetic local publication interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(source_url_uploads, "_finish", fail_upload_publication)


def test_generation_upload_uses_url_without_legacy_object_upload(
    url_env, redis_client, wire
):
    wire[1].append(
        [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]
    )
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "succeeded"
    assert op.remote_response["video_id"] == "actual-source-vid"
    assert dict(wire[0][0][2]["fields"])["upload_type"] == "UPLOAD_BY_URL"
    assert "video_file" not in dict(wire[0][0][2]["fields"])


def test_success_receipt_releases_use_and_source_slot_without_readback(
    url_env, redis_client, wire
):
    wire[1].append(
        [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]
    )
    run(url_env, redis_client)
    op_id = operation(url_env).id
    # 重复投递及原来已排队的恢复消息都不能查询或再次上传已成功素材。
    run(url_env, redis_client, kind="verify", operation_id=op_id)
    run(url_env, redis_client, kind="verify", operation_id=op_id)
    with Session(engine) as db:
        op = db.get(MaterialAssetOperation, op_id)
        asset = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == url_env["material_id"]
            )
        ).one()
        assert (op.status, asset.video_id, asset.mid) == (
            "succeeded",
            "actual-source-vid",
            "actual-source-mid",
        )
        assert (
            db.exec(
                select(OriginalUse.status).where(OriginalUse.operation_id == op_id)
            ).one()
            == "released"
        )
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            == 0
        )
        assert db.get(IngestSession, url_env["session_id"]).ready_count == 1
        from app.modules.materials.ingest_models import ObjectCleanup

        cleanup = db.exec(
            select(ObjectCleanup).where(
                ObjectCleanup.tenant_id == url_env["context"].tenant_id
            )
        ).one()
        assert cleanup.eligibility_evidence["source_receipt_id"] == str(op_id)
        assert (
            db.get(PendingDispatch, cleanup.dispatch_id).task_name
            == "materials.cleanup_original"
        )
        assert db.get(IngestSession, url_env["session_id"]).ready_bytes == len(CONTENT)
        assert (
            db.get(TemporaryMaterialObject, url_env["object_id"]).status
            == "cleanup_pending"
        )
        evidence = repr(op.remote_response) + repr(
            db.exec(
                select(MaterialUploadAttempt).where(
                    MaterialUploadAttempt.operation_id == op_id
                )
            )
            .one()
            .remote_response
        )
        dispatches = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == url_env["context"].tenant_id
            )
        ).all()
        assert "never-store-this-url" not in evidence + repr(
            [d.payload for d in dispatches]
        )
    assert [call[0] for call in wire[0]] == ["POST"]


@pytest.mark.parametrize(
    "response",
    [
        {"list": []},
        info(video_id="wrong-vid"),
        info(signature="a" * 32),
        info(displayable=False),
        info(size=999),
        info(width=None),
        info(duration=True),
        info(format="html"),
    ],
)
@pytest.mark.usefixtures("interrupted_publication")
def test_unusable_readback_never_marks_ready_or_releases_original(
    url_env, redis_client, wire, response
):
    wire[1].extend([[{"video_id": "actual-source-vid"}], response])
    run(url_env, redis_client)
    op_id = operation(url_env).id
    run(url_env, redis_client, kind="verify", operation_id=op_id)
    run(url_env, redis_client, operation_id=op_id)
    with Session(engine) as db:
        assert db.get(MaterialAssetOperation, op_id).status == "verifying"
        assert db.get(IngestSession, url_env["session_id"]).ready_count == 0
        assert (
            db.exec(
                select(OriginalUse.status).where(OriginalUse.operation_id == op_id)
            ).one()
            == "active"
        )
    assert [call[0] for call in wire[0]] == ["POST", "GET"]


def test_timeout_recovers_same_name_digest_account_without_second_post(
    url_env, redis_client, wire, caplog
):
    wire[1].append(ReadTimeoutError(None, URL, "never-store-this-url"))
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "result_unknown"
    with Session(engine) as db:
        use = db.exec(
            select(OriginalUse).where(OriginalUse.operation_id == op.id)
        ).one()
        use.expires_at = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()
    # Expired presign/use clock and switched-off new ingest do not authorize reupload.
    settings.__dict__["MATERIAL_INGEST_ENABLED"] = False
    candidate = info()["list"][0]
    wire[1].append(
        {
            "list": [
                {**candidate, "file_name": op.remote_response["remote_name"]},
                {
                    **candidate,
                    "video_id": "same-name-wrong-content",
                    "signature": "a" * 32,
                    "file_name": op.remote_response["remote_name"],
                },
            ],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 2,
            },
        }
    )
    run(url_env, redis_client, operation_id=op.id)
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    wire[1].append(info())
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    assert operation(url_env).status == "succeeded"
    assert [call[0] for call in wire[0]] == ["POST", "GET", "GET"]
    assert (
        len({dict(call[2].get("fields", []))["advertiser_id"] for call in wire[0]}) == 1
    )
    with Session(engine) as db:
        persisted = [operation(url_env).remote_response]
        persisted += [
            attempt.remote_response
            for attempt in db.exec(
                select(MaterialUploadAttempt).where(
                    MaterialUploadAttempt.operation_id == op.id
                )
            ).all()
        ]
        persisted += [
            dispatch.payload
            for dispatch in db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == url_env["context"].tenant_id
                )
            ).all()
        ]
    assert URL not in repr(persisted) + caplog.text
    assert "never-store-this-url" not in repr(persisted) + caplog.text


def test_received_id_is_saved_before_client_cleanup_failure(
    url_env, redis_client, wire, monkeypatch
):
    import urllib3

    original_clear = urllib3.PoolManager.clear

    def fail_after_receipt(pool):
        original_clear(pool)
        assert operation(url_env).remote_response["video_id"] == "actual-source-vid"
        raise RuntimeError("cleanup includes never-store-this-url")

    monkeypatch.setattr(urllib3.PoolManager, "clear", fail_after_receipt)
    wire[1].append(
        [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]
    )
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.remote_response["video_id"] == "actual-source-vid"
    assert op.status == "succeeded"
    with Session(engine) as db:
        attempt = db.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == op.id
            )
        ).one()
        assert attempt.remote_response["upload_video_id"] == "actual-source-vid"
        assert attempt.remote_response["upload_mid"] == "actual-source-mid"
        assert "never-store-this-url" not in repr(attempt.remote_response)


def test_revocation_after_post_keeps_receipt_and_does_not_release_use(
    url_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess

    def revoke_and_reply():
        with Session(engine) as db, db.begin():
            grant = db.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            grant.authorized = False
        return [{"video_id": "actual-source-vid"}]

    wire[1].append(revoke_and_reply)
    run(url_env, redis_client)
    op = operation(url_env)
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    assert operation(url_env).remote_response["video_id"] == "actual-source-vid"
    with Session(engine) as db:
        assert (
            db.exec(
                select(OriginalUse.status).where(OriginalUse.operation_id == op.id)
            ).one()
            == "active"
        )
        assert db.get(IngestSession, url_env["session_id"]).ready_count == 0
    assert len(wire[0]) == 1


@pytest.mark.parametrize(
    "mutation", ["missing_digest", "wrong_digest", "too_large", "disabled"]
)
def test_unready_original_never_claims_source_or_signs(
    url_env, redis_client, wire, mutation
):
    with Session(engine) as db, db.begin():
        material = db.get(MaterialFile, url_env["material_id"])
        if mutation == "missing_digest":
            material.digest_verified_at = None
        elif mutation == "wrong_digest":
            material.video_md5 = "b" * 32
    if mutation == "too_large":
        settings.__dict__["MATERIAL_URL_MAX_UPLOAD_BYTES"] = 1
    if mutation == "disabled":
        settings.__dict__["MATERIAL_INGEST_ENABLED"] = False
    run(url_env, redis_client)
    with Session(engine) as db:
        assert (
            db.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.material_id == url_env["material_id"]
                )
            ).first()
            is None
        )
        assert (
            db.exec(
                select(OriginalUse).where(
                    OriginalUse.material_id == url_env["material_id"]
                )
            ).first()
            is None
        )
        assert (
            db.exec(
                select(SourceAccountLoad).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).first()
            is None
        )
        assert (
            db.exec(
                select(IngestSessionFile.status).where(
                    IngestSessionFile.material_id == url_env["material_id"]
                )
            ).one()
            == "blocked"
        )
    assert wire[0] == []


def test_proven_unsent_admission_denial_releases_temporary_use_and_keeps_original(
    url_env, redis_client, wire
):
    from app.jobs.admission import admission_policy, admit_call, release_call

    lease_id = uuid4()
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": "materials.upload_video_url",
        "tenant_id": url_env["context"].tenant_id,
        "advertiser_id": "actual-account",
        "lease_id": lease_id,
    }
    assert admit_call(
        redis_client, **scope, policy=admission_policy("materials.upload_video_url")
    ).granted
    try:
        run(url_env, redis_client)
    finally:
        release_call(redis_client, **scope)
    op = operation(url_env)
    assert op.status == "pending" and not op.remote_response["send_armed"]
    with Session(engine) as db:
        use = db.exec(
            select(OriginalUse).where(OriginalUse.operation_id == op.id)
        ).one()
        assert use.released_at is not None
        obj = db.get(TemporaryMaterialObject, url_env["object_id"])
        assert obj.reservation_released_at is None and obj.reserved_bytes == len(
            CONTENT
        )
        file = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == url_env["material_id"]
            )
        ).one()
        dispatch = db.get(PendingDispatch, file.dispatch_id)
        assert dispatch.payload["object_id"] == str(url_env["object_id"])
        assert dispatch.payload["generation"] == 1
        assert dispatch.payload["operation_id"] == str(op.id)
    assert wire[0] == []


def test_signing_failure_is_proven_unsent_and_releases_claimed_source(
    url_env, redis_client, wire, monkeypatch
):
    from app.modules.materials import source_url_uploads

    def unavailable(**_kwargs):
        raise RuntimeError(URL)

    monkeypatch.setattr(source_url_uploads, "sign_ingest_url", unavailable)
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "failed" and not op.remote_response["send_armed"]
    with Session(engine) as db:
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            == 0
        )
    assert wire[0] == []


def test_generation_material_without_exact_pair_never_falls_back_to_file(
    url_env, redis_client, wire
):
    from app.core.errors import DomainError

    with pytest.raises(DomainError) as error:
        run_source_upload(
            database_engine=engine,
            redis_client=redis_client,
            context=url_env["context"],
            material_id=url_env["material_id"],
        )
    assert error.value.code == "invalid_asset_task"
    assert wire[0] == []


@pytest.mark.parametrize(
    "original", ["月光 Episode 07.mp4", "a" * 90 + ".mp4", "月光" * 50 + ".mp4"]
)
def test_remote_name_preserves_original_name_and_stable_unique_suffix(
    url_env, redis_client, wire, original
):
    with Session(engine) as db, db.begin():
        db.get(MaterialFile, url_env["material_id"]).file_name = original
    wire[1].append([{"video_id": "actual-source-vid"}])
    run(url_env, redis_client)
    name = operation(url_env).remote_response["remote_name"]
    assert name.startswith(original[:2])
    assert len(name.encode("utf-8")) <= 100
    assert name.endswith(".mp4")
    assert len(name.rsplit("-", 1)[-1].removesuffix(".mp4")) == 8
    assert dict(wire[0][0][2]["fields"])["file_name"] == name


def advance_generation(env):
    with Session(engine) as db, db.begin():
        old = db.get(TemporaryMaterialObject, env["object_id"])
        old.status = "deleted"
        old.deleted_at = old.reservation_released_at = datetime.now(UTC)
        material = db.get(MaterialFile, env["material_id"])
        material.current_object_generation = 2
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == material.id
            )
        ).one()
        row.current_generation = 2
        row.status = "stored"
        new = TemporaryMaterialObject(
            **{
                **old.model_dump(
                    exclude={
                        "id",
                        "generation",
                        "object_key",
                        "status",
                        "deleted_at",
                        "reservation_released_at",
                    }
                ),
                "generation": 2,
                "object_key": old.object_key + "/2",
                "status": "verified",
            }
        )
        db.add(new)
        db.flush()
        return {
            **env,
            "object_id": new.id,
            "generation": 2,
            "object_key": new.object_key,
        }


def test_confirmed_failed_deleted_generation_allows_new_charged_source_operation(
    url_env, redis_client, wire, monkeypatch
):
    from app.modules.materials import source_url_uploads

    original_signer = source_url_uploads.sign_ingest_url

    def unavailable(**_kwargs):
        raise RuntimeError("test signing unavailable")

    monkeypatch.setattr(source_url_uploads, "sign_ingest_url", unavailable)
    run(url_env, redis_client)
    old = operation(url_env)
    assert old.status == "failed" and not old.remote_response["source_slot_held"]
    next_env = advance_generation(url_env)

    class NextSigner:
        def generate_presigned_url(self, method, **kwargs):
            assert kwargs["Params"]["Key"] == next_env["object_key"]
            return URL

    next_env["s3"] = NextSigner()
    monkeypatch.setattr(source_url_uploads, "sign_ingest_url", original_signer)
    wire[1].append([{"video_id": "actual-source-vid"}])
    run(next_env, redis_client)
    with Session(engine) as db:
        ops = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == url_env["material_id"]
            )
        ).all()
        assert len(ops) == 2
        new = next(op for op in ops if op.id != old.id)
        assert new.remote_response["generation"] == 2
        assert new.remote_response["source_slot_held"] is False
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            == 0
        )
    assert len(wire[0]) == 1


def test_unknown_previous_generation_never_creates_new_source_operation(
    url_env, redis_client, wire
):
    wire[1].append(ReadTimeoutError(None, URL, "uncertain"))
    run(url_env, redis_client)
    old = operation(url_env)
    assert old.remote_response["send_armed"]
    next_env = advance_generation(url_env)  # Even corrupt upstream state fails closed.
    run(next_env, redis_client)
    assert operation(url_env).id == old.id
    assert len(wire[0]) == 1


def test_concurrent_duplicate_dispatch_cannot_send_second_post(
    url_env, redis_client, wire
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, finish = Event(), Event()

    def pending_reply():
        entered.set()
        assert finish.wait(10)
        return [{"video_id": "actual-source-vid"}]

    wire[1].append(pending_reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, url_env, redis_client)
        try:
            assert entered.wait(10)
            duplicate = pool.submit(run, url_env, redis_client)
            duplicate.result(timeout=10)
            assert len(wire[0]) == 1
        finally:
            finish.set()
        first.result(timeout=10)
    assert operation(url_env).remote_response["video_id"] == "actual-source-vid"
    with Session(engine) as db:
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            == 0
        )


def test_late_upload_receipt_keeps_actual_ids_without_overwriting_successor_claim(
    url_env, redis_client, wire
):
    successor = uuid4()

    def takeover_and_reply():
        with Session(engine) as db, db.begin():
            op = db.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.material_id == url_env["material_id"]
                )
            ).one()
            op.status = "result_unknown"
            op.attempt_token = successor
            op.claimed_until = datetime.now(UTC) + timedelta(seconds=60)
        return [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]

    wire[1].append(takeover_and_reply)
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.attempt_token == successor
    assert op.status == "result_unknown"
    assert op.remote_response["video_id"] == "actual-source-vid"
    with Session(engine) as db:
        attempt = db.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.operation_id == op.id
            )
        ).one()
        assert attempt.remote_response["upload_video_id"] == "actual-source-vid"
        assert attempt.remote_response["upload_mid"] == "actual-source-mid"
        assert (
            db.exec(
                select(OriginalUse.status).where(OriginalUse.operation_id == op.id)
            ).one()
            == "active"
        )
        assert db.get(IngestSession, url_env["session_id"]).ready_count == 0


def test_explicit_retry_reclaims_released_slot_and_cannot_retry_unknown(
    url_env, redis_client, wire, monkeypatch
):
    from app.core.errors import DomainError
    from app.modules.materials import source_url_uploads
    from app.modules.materials.source_uploads import request_source_retry

    signer = source_url_uploads.sign_ingest_url

    def fail_before_post(**_kwargs):
        raise RuntimeError("not sent")

    monkeypatch.setattr(source_url_uploads, "sign_ingest_url", fail_before_post)
    run(url_env, redis_client)
    first = operation(url_env)
    with Session(engine) as db, db.begin():
        dispatch_id = request_source_retry(
            db, context=url_env["context"], material_id=url_env["material_id"]
        )
        dispatch = db.get(PendingDispatch, dispatch_id)
        next_id = dispatch.payload["operation_id"]
        assert next_id != str(first.id)
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == url_env["context"].tenant_id
                )
            ).one()
            == 1
        )
    monkeypatch.setattr(source_url_uploads, "sign_ingest_url", signer)
    wire[1].append(ReadTimeoutError(None, URL, "unknown"))
    from uuid import UUID

    run(url_env, redis_client, operation_id=UUID(next_id))
    with Session(engine) as db, db.begin():
        with pytest.raises(DomainError) as error:
            request_source_retry(
                db, context=url_env["context"], material_id=url_env["material_id"]
            )
        assert error.value.code == "material_retry_not_allowed"
    assert len(wire[0]) == 1


def test_source_wrapper_accepts_exact_validation_payload(url_env, monkeypatch):
    from app.modules.materials import tasks

    calls = []
    monkeypatch.setattr(tasks, "require_bounded_worker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tasks, "run_source_upload", lambda **kwargs: calls.append(kwargs)
    )
    tasks._run(
        None,
        kind="upload",
        hard_limit=900,
        tenant_id=str(url_env["context"].tenant_id),
        actor_id=str(url_env["context"].actor_id),
        payload={
            "material_id": str(url_env["material_id"]),
            "object_id": str(url_env["object_id"]),
            "generation": 1,
        },
    )
    assert calls[0]["object_id"] == url_env["object_id"]
    assert calls[0]["generation"] == 1
    assert calls[0]["operation_id"] is None


@pytest.mark.usefixtures("interrupted_publication")
def test_conflicting_late_receipt_cannot_become_ready_from_single_id_readback(
    url_env, redis_client, wire
):
    from app.modules.materials.source_url_uploads import _receipt

    wire[1].append([{"video_id": "actual-source-vid"}])
    run(url_env, redis_client)
    op = operation(url_env)
    _receipt(
        engine,
        context=url_env["context"],
        material_id=url_env["material_id"],
        object_id=url_env["object_id"],
        generation=1,
        operation_id=op.id,
        claim=uuid4(),
        evidence={"video_id": "other-actual-vid"},
    )
    wire[1].append(info())
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    op = operation(url_env)
    assert op.status != "succeeded"
    assert op.remote_response["video_id"] == "actual-source-vid"
    assert op.remote_response["conflicting_video_id"] == "other-actual-vid"
    with Session(engine) as db:
        assert db.get(IngestSession, url_env["session_id"]).ready_count == 0
        assert (
            db.exec(
                select(OriginalUse.status).where(OriginalUse.operation_id == op.id)
            ).one()
            == "active"
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"object_id": "unused"},
        {"generation": 1},
        {"object_id": "unused", "generation": True},
    ],
)
def test_source_wrapper_rejects_partial_or_boolean_generation(
    url_env, monkeypatch, extra
):
    from app.core.errors import DomainError
    from app.modules.materials import tasks

    monkeypatch.setattr(tasks, "require_bounded_worker", lambda *_args, **_kwargs: None)
    with pytest.raises(DomainError) as error:
        tasks._run(
            None,
            kind="upload",
            hard_limit=900,
            tenant_id=str(url_env["context"].tenant_id),
            actor_id=str(url_env["context"].actor_id),
            payload={"material_id": str(url_env["material_id"]), **extra},
        )
    assert error.value.code == "invalid_asset_task"


def test_received_vid_commit_failure_retries_receipt_without_another_post(
    url_env, redis_client, wire, monkeypatch
):
    import urllib3
    from sqlalchemy import event

    failed, closed_with_receipt = [], []
    original_clear = urllib3.PoolManager.clear

    def fail_receipt_once(_conn, _cursor, statement, _params, _context, _many):
        if (
            "UPDATE material_asset_operation SET" in statement
            and len(wire[0]) == 1
            and not failed
        ):
            failed.append(True)
            raise RuntimeError("synthetic receipt transaction failure")

    def clear(pool):
        if wire[0]:
            assert (
                operation(url_env).remote_response.get("video_id")
                == "actual-source-vid"
            )
            closed_with_receipt.append(True)
        original_clear(pool)

    monkeypatch.setattr(urllib3.PoolManager, "clear", clear)
    event.listen(engine, "before_cursor_execute", fail_receipt_once)
    try:
        wire[1].append(
            [{"video_id": "actual-source-vid", "material_id": "actual-source-mid"}]
        )
        run(url_env, redis_client)
    finally:
        event.remove(engine, "before_cursor_execute", fail_receipt_once)
    assert failed and closed_with_receipt
    op = operation(url_env)
    assert op.remote_response["video_id"] == "actual-source-vid"
    assert op.remote_response["mid"] == "actual-source-mid"
    assert [call[0] for call in wire[0]] == ["POST"]


def test_two_receipt_failures_keep_unknown_intent_and_do_not_repeat_post(
    url_env, redis_client, wire
):
    from sqlalchemy import event

    failures = []

    def reject_receipts(_conn, _cursor, statement, _params, _context, _many):
        if (
            "UPDATE material_asset_operation SET" in statement
            and wire[0]
            and len(failures) < 2
        ):
            failures.append(True)
            raise RuntimeError(URL)

    event.listen(engine, "before_cursor_execute", reject_receipts)
    try:
        wire[1].append([{"video_id": "actual-source-vid"}])
        run(url_env, redis_client)
    finally:
        event.remove(engine, "before_cursor_execute", reject_receipts)
    assert len(failures) == 2
    op = operation(url_env)
    assert op.status == "result_unknown" and op.remote_response["send_armed"] is True
    assert URL not in repr(op.remote_response)
    run(url_env, redis_client)
    assert [call[0] for call in wire[0]] == ["POST"]
