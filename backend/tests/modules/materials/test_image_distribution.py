"""图片主资产真实 PG/Redis 状态机；平台仅在 SDK 传输边界替换。"""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.routing import freeze_route
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
)
from tests.modules.materials.test_distribution import run, state
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_source_uploads import MD5
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)
from tests.modules.materials.test_source_uploads import (
    wire as wire,
)
from tests.modules.materials.test_url_ingest import url_env as url_env


@pytest.fixture
def image_env(source_env, monkeypatch):
    from app.core.config import settings
    from app.modules.accounts.connection_models import ConnectionAuthorization

    with Session(engine) as db, db.begin():
        material = db.get(MaterialFile, source_env["material_id"])
        material.media_type, material.mime_type, material.file_name = (
            "IMAGE",
            "image/png",
            "Moon.png",
        )
        material.width, material.height, material.duration = 900, 1600, None
        material.digest_verified_at = datetime.now(UTC)
        auth = db.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == source_env["connection_id"]
            )
        ).one()
        auth.scopes = ["6"]
        target(db, source_env)
        target(db, source_env, advertiser_id="target-two")
    from tests.modules.materials.test_covers import scopes

    scopes(source_env)
    policies = settings.TIKTOK_CALL_POLICIES
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **policies,
            "endpoints": {
                **policies["endpoints"],
                "materials.upload_image_url": {"lease_ms": 970000},
            },
        },
    )
    return source_env


def seed_image(env, advertiser_id="actual-account"):
    with Session(engine) as db, db.begin():
        db.add(
            AccountMaterial(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                material_id=env["material_id"],
                advertiser_id=advertiser_id,
                connection_id=env["connection_id"],
                media_type="IMAGE",
                video_id=None,
                frozen_route=freeze_route(
                    db,
                    context=env["context"],
                    bc_id=env["bc_id"],
                    connection_id=env["connection_id"],
                ).model_dump(mode="json"),
                image_id="source-image",
                mid="source-image-mid",
                is_carousel_usable=True,
                status="available",
                verified_at=datetime.now(UTC),
            )
        )


def queue_images(env, accounts=("target-account",)):
    from app.modules.materials.image_distribution import prepare_image_targets

    with Session(engine) as db, db.begin():
        route = freeze_route(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        )
        return prepare_image_targets(
            route,
            (env["material_id"], env["material_id"]),
            accounts,
            session=db,
            context=env["context"],
        )


def image_info(identity="source-image", *, usable=True):
    return {
        "list": [
            {
                "image_id": identity,
                "material_id": "source-image-mid",
                "signature": MD5,
                "file_name": "Moon.png",
                "width": 900,
                "height": 1600,
                "displayable": False,
                "is_carousel_usable": usable,
            }
        ]
    }


def test_share_receipt_never_ready_and_repeated_carousels_share_one_task(
    image_env, wire, redis_client
):
    seed_image(image_env)
    result = queue_images(image_env)
    again = queue_images(image_env)
    assert len(result.items) == 1 and result.items == again.items
    task = result.items[0].task_id
    wire[1].extend([image_info(), {}])
    run(image_env, redis_client, task, kind="prepare")
    assert state(task)[0].status == "verifying" and state(task)[2] is None, state(task)[
        1
    ].remote_response
    wire[1].append(image_info())
    run(image_env, redis_client, task)
    dist, op, mapping = state(task)
    assert dist.status == "ready" and mapping.video_id is None
    assert mapping.image_id == "source-image" and mapping.is_carousel_usable is True
    run(image_env, redis_client, task, kind="prepare")
    assert [c[0] for c in wire[0]] == ["GET", "POST", "GET"]
    from app.modules.materials.cover_models import MaterialCoverJob as CoverJob

    with Session(engine) as db:
        assert not db.exec(
            select(CoverJob).where(CoverJob.material_id == image_env["material_id"])
        ).all()


@pytest.mark.parametrize("usable", [False, None])
def test_carousel_explicit_true_required_and_target_failure_isolated(
    image_env, wire, redis_client, usable
):
    seed_image(image_env)
    result = queue_images(image_env, ("target-account", "target-two"))
    first, second = (item.task_id for item in result.items)
    wire[1].extend([image_info(), {}, image_info(usable=usable)])
    run(image_env, redis_client, first, kind="prepare")
    run(image_env, redis_client, first)
    assert state(first)[0].status != "ready" and state(first)[2] is None
    wire[1].extend([image_info(), {}, image_info()])
    run(image_env, redis_client, second, kind="prepare")
    run(image_env, redis_client, second)
    assert state(second)[0].status == "ready"


def test_late_result_after_binding_revision_change_is_not_published(
    image_env, wire, redis_client
):
    from app.modules.accounts.connection_models import BCConnectionBinding

    seed_image(image_env)
    task = queue_images(image_env).items[0].task_id
    wire[1].extend([image_info(), {}])
    run(image_env, redis_client, task, kind="prepare")
    with Session(engine) as db, db.begin():
        binding = db.exec(
            select(BCConnectionBinding).where(
                BCConnectionBinding.connection_id == image_env["connection_id"]
            )
        ).one()
        binding.revision += 1
    wire[1].append(image_info())
    run(image_env, redis_client, task)
    assert state(task)[0].status != "ready" and state(task)[2] is None


def revoke_build(env):
    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as db, db.begin():
        grant = db.get(
            BCAccountAccess,
            (
                env["context"].tenant_id,
                env["bc_id"],
                "target-account",
                env["connection_id"],
            ),
        )
        assert grant.authorized and grant.can_upload
        grant.can_build = False


@pytest.mark.parametrize("path", ["share", "original"])
def test_target_build_revoked_after_queue_never_sends(
    image_url_env, wire, redis_client, path
):
    env = image_url_env
    if path == "share":
        seed_image(env)
        wire[1].extend([image_info(), {}])
    else:
        wire[1].append({"image_id": "target-image", "material_id": "target-mid"})
    task = queue_images(env).items[0].task_id
    revoke_build(env)
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    assert not wire[0]
    assert state(task)[0].status == "blocked" and state(task)[2] is None


def test_target_build_revoked_during_readback_never_publishes(
    image_env, wire, redis_client
):
    seed_image(image_env)
    task = queue_images(image_env).items[0].task_id
    wire[1].extend([image_info(), {}])
    run(image_env, redis_client, task, kind="prepare")

    def revoked():
        revoke_build(image_env)
        return image_info()

    wire[1].append(revoked)
    run(image_env, redis_client, task)
    assert state(task)[2] is None
    assert state(task)[0].status == "result_unknown"


@pytest.fixture
def image_url_env(image_env, url_env):
    from app.modules.materials.ingest_models import IngestSessionFile

    assert image_env["material_id"] == url_env["material_id"]
    with Session(engine) as db, db.begin():
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == url_env["material_id"]
            )
        ).one()
        row.media_type, row.status = "IMAGE", "stored"
    return url_env


def queue_original(env):
    from app.jobs.models import PendingDispatch
    from app.modules.materials.image_distribution import enqueue_image_original
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        TemporaryMaterialObject,
    )

    with Session(engine) as db, db.begin():
        material = db.get(MaterialFile, env["material_id"])
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == material.id
            )
        ).one()
        dispatch = enqueue_image_original(
            db,
            context=env["context"],
            material=material,
            obj=db.get(TemporaryMaterialObject, env["object_id"]),
            row=row,
        )
        return __import__("uuid").UUID(
            db.get(PendingDispatch, dispatch).payload["distribution_id"]
        )


def test_original_upload_readback_cleanup_and_unknown_never_reuploads(
    image_url_env, redis_client, wire
):
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        ObjectCleanup,
        OriginalUse,
    )

    env = image_url_env
    task = queue_original(env)
    assert queue_original(env) == task
    wire[1].append({"image_id": "uploaded-image", "material_id": "uploaded-mid"})
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    assert state(task)[0].status == "verifying" and state(task)[2] is None
    # 平台转换 PNG 为 JPEG 后签名可变；已知回执ID不依赖原件MD5。
    response = image_info("uploaded-image")
    response["list"][0].update(signature="f" * 32, format="jpeg")
    wire[1].append(response)
    run(env, redis_client, task)
    assert state(task)[0].status == "ready", state(task)[1].remote_response
    with Session(engine) as db:
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == env["material_id"]
            )
        ).one()
        assert row.status == "available"
        assert (
            db.exec(
                select(OriginalUse).where(
                    OriginalUse.material_id == env["material_id"],
                    OriginalUse.status == "active",
                )
            ).all()
            == []
        )
        cleanup = db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).one()
        assert (
            cleanup.reason == "verified_source"
            and cleanup.eligibility_evidence["image_id"] == "uploaded-image"
        )
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    assert len(wire[0]) == 2


def test_direct_target_original_readback_releases_its_use(
    image_url_env, redis_client, wire
):
    from app.modules.materials.ingest_models import OriginalUse

    env = image_url_env
    task = queue_images(env).items[0].task_id
    wire[1].append({"image_id": "target-image", "material_id": "target-mid"})
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    with Session(engine) as db:
        assert db.exec(
            select(OriginalUse).where(
                OriginalUse.material_id == env["material_id"],
                OriginalUse.status == "active",
            )
        ).all()
    wire[1].append(image_info("target-image"))
    run(env, redis_client, task)
    assert state(task)[0].status == "ready"
    with Session(engine) as db:
        assert not db.exec(
            select(OriginalUse).where(
                OriginalUse.material_id == env["material_id"],
                OriginalUse.status == "active",
            )
        ).all()


def test_unknown_upload_search_is_bounded_and_no_second_post(
    image_url_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    env = image_url_env
    task = queue_original(env)
    wire[1].append(ReadTimeoutError(None, "https://offline.invalid", "secret"))
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    assert state(task)[0].status == "result_unknown"
    wire[1].append(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 0,
                "total_number": 0,
            },
        }
    )
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    assert [call[0] for call in wire[0]] == ["POST", "GET"]
    assert state(task)[1].remote_response["reconciliation_complete"] is True


def test_fresh_scoped_image_preview_is_transient_and_strict(
    image_env, wire, redis_client
):
    from datetime import timedelta

    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.materials import RemoteCallBudget
    from app.integrations.tiktok.gateway import open_tiktok_gateway

    with Session(engine) as db:
        route = freeze_route(
            db,
            context=image_env["context"],
            bc_id=image_env["bc_id"],
            connection_id=image_env["connection_id"],
        )
    deadline = datetime.now(UTC) + timedelta(seconds=40)
    budget = RemoteCallBudget(deadline, 45, 60000)
    data = image_info()
    data["list"][0]["image_url"] = (
        "https://image.example.invalid/preview?signature=secret"
    )
    wire[1].append(data)
    with open_tiktok_gateway(
        database_engine=engine,
        redis_client=redis_client,
        context=image_env["context"],
        route=route,
        task_deadline=deadline,
    ) as gateway:
        preview = gateway.materials.read_image_preview(
            advertiser_id="actual-account", image_id="source-image", budget=budget
        )
        assert preview.image_url == data["list"][0][
            "image_url"
        ] and "secret" not in repr(preview)
        wire[1].append(image_info())
        with pytest.raises(DomainError):
            gateway.materials.read_image_preview(
                advertiser_id="actual-account", image_id="source-image", budget=budget
            )


def test_validator_handoff_is_real_task_and_recovery_is_idempotent(
    image_env, monkeypatch
):
    from io import BytesIO
    from uuid import uuid4

    from PIL import Image

    from app.core.config import settings
    from app.jobs.models import PendingDispatch
    from app.modules.materials.image_distribution import repair_image_handoffs
    from app.modules.materials.ingest_models import (
        IngestSession,
        IngestSessionFile,
        TemporaryMaterialObject,
        canonical_object_key,
        record_milestone,
    )
    from app.modules.materials.object_validation import enqueue_validation
    from tests.modules.materials.test_validation_dispatch import FakeStorage
    from tests.modules.materials.test_validation_retry_bounds import invoke

    monkeypatch.setattr(settings, "MATERIAL_INGEST_ENABLED", True)
    env = image_env
    buf = BytesIO()
    Image.new("RGB", (32, 24), "red").save(buf, format="PNG")
    content = buf.getvalue()
    with Session(engine) as db, db.begin():
        material = MaterialFile(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            file_name="real.png",
            media_type="IMAGE",
            mime_type="image/png",
            byte_size=len(content),
            object_key=str(uuid4()),
            current_object_generation=1,
        )
        db.add(material)
        db.flush()
        route = freeze_route(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        )
        parent = IngestSession(
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            actor_id=env["context"].actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            expected_files=1,
            expected_bytes=len(content),
            frozen_route=route.model_dump(mode="json"),
        )
        db.add(parent)
        db.flush()
        row = IngestSessionFile(
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            session_id=parent.id,
            client_index=0,
            material_id=material.id,
            media_type="IMAGE",
            byte_size=len(content),
            manifest_digest="b" * 64,
        )
        obj = TemporaryMaterialObject(
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            material_id=material.id,
            generation=1,
            object_key=canonical_object_key(
                material.tenant_id, material.bc_id, material.id, 1
            ),
            expected_bytes=len(content),
            actual_bytes=len(content),
            reserved_bytes=len(content),
            status="stored",
            storage_provider="r2",
            storage_endpoint="https://owned.r2.example",
            storage_bucket="test-bucket",
        )
        db.add_all([row, obj])
        db.flush()
        record_milestone(
            db,
            tenant_id=material.tenant_id,
            bc_id=material.bc_id,
            session_id=parent.id,
            material_id=material.id,
            milestone="accepted",
        )
        dispatch = enqueue_validation(db, context=env["context"], object_id=obj.id)
        case = env["context"], obj.id, material.id, parent.id, row.id, dispatch
        storage = FakeStorage(obj, material, body=content)
        db.expunge(obj)
        db.expunge(material)
    invoke(case, storage)
    with Session(engine) as db, db.begin():
        row = db.get(IngestSessionFile, case[4])
        message = db.get(PendingDispatch, row.dispatch_id)
        assert message.task_name == "materials.prepare_target", (
            row.status,
            row.error_code,
            db.get(TemporaryMaterialObject, case[1]).error_code,
        )
        assert message.id != dispatch
        assert repair_image_handoffs(db) == 0
        assert (
            len(
                db.exec(
                    select(MaterialAssetOperation).where(
                        MaterialAssetOperation.material_id == case[2]
                    )
                ).all()
            )
            == 1
        )


def test_historic_handoff_and_lost_outbox_recover_once(image_url_env):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch
    from app.modules.materials.distribution import repair_material_dispatches
    from app.modules.materials.image_distribution import repair_image_handoffs
    from app.modules.materials.ingest_models import IngestSessionFile

    env = image_url_env
    with Session(engine) as db, db.begin():
        assert repair_image_handoffs(db) == 1
        assert repair_image_handoffs(db) == 0
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == env["material_id"]
            )
        ).one()
        message = db.get(PendingDispatch, row.dispatch_id)
        message.published_at = message.available_at = datetime.now(UTC) - timedelta(
            minutes=5
        )
        identity = message.id
    with Session(engine) as db, db.begin():
        assert repair_material_dispatches(db) >= 1
        assert db.get(PendingDispatch, identity).published_at is None
        assert repair_material_dispatches(db) == 0


def test_concurrent_carousels_reserve_one_account_task(image_env):
    from concurrent.futures import ThreadPoolExecutor

    seed_image(image_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: queue_images(image_env), range(2)))
    assert results[0].items[0].task_id == results[1].items[0].task_id


def test_binding_changes_during_response_preserve_receipt_without_ready(
    image_env, wire, redis_client
):
    from app.modules.accounts.connection_models import BCConnectionBinding

    seed_image(image_env)
    task = queue_images(image_env).items[0].task_id
    wire[1].extend([image_info(), {}])
    run(image_env, redis_client, task, kind="prepare")

    def changed():
        with Session(engine) as db, db.begin():
            binding = db.exec(
                select(BCConnectionBinding).where(
                    BCConnectionBinding.connection_id == image_env["connection_id"]
                )
            ).one()
            binding.revision += 1
        return image_info()

    wire[1].append(changed)
    run(image_env, redis_client, task)
    dist, op, mapping = state(task)
    assert mapping is None and dist.status == "result_unknown"
    assert op.remote_response["image_id"] == "source-image"


def test_readiness_and_public_preparation_use_image_identity(
    image_env, wire, redis_client
):
    from tests.modules.materials.test_distribution import queue
    from tests.modules.materials.test_readiness import read

    seed_image(image_env)
    prepared = queue(image_env, "target-account")
    wire[1].extend([image_info(), {}, image_info()])
    run(image_env, redis_client, prepared.task_id, kind="prepare")
    run(image_env, redis_client, prepared.task_id)
    ready = read(image_env, "target-account")
    assert ready.state == "ready" and ready.mapping.media_type == "IMAGE"
    assert ready.mapping.video_id is None


def test_unusable_source_releases_read_use_and_allows_cancel(
    image_url_env, redis_client, wire
):
    from app.modules.materials.ingest_models import (
        IngestSessionFile,
        OriginalUse,
        SourceAccountLoad,
    )

    env = image_url_env
    task = queue_original(env)
    wire[1].extend([{"image_id": "unusable"}, image_info("unusable", usable=False)])
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    run(env, redis_client, task)
    dist, op, mapping = state(task)
    assert dist.status == "blocked" and mapping is None
    assert op.remote_response["image_read_complete"] is True
    with Session(engine) as db:
        assert not db.exec(
            select(OriginalUse).where(
                OriginalUse.material_id == env["material_id"],
                OriginalUse.status == "active",
            )
        ).all()
        assert (
            db.exec(
                select(IngestSessionFile).where(
                    IngestSessionFile.material_id == env["material_id"]
                )
            )
            .one()
            .status
            == "failed"
        )
        assert (
            db.exec(
                select(SourceAccountLoad).where(
                    SourceAccountLoad.tenant_id == env["context"].tenant_id
                )
            )
            .one()
            .in_flight
            == 0
        )


def test_source_without_mid_keeps_original_for_future_distribution(
    image_url_env, wire, redis_client
):
    from app.modules.materials.ingest_models import ObjectCleanup

    env = image_url_env
    task = queue_original(env)
    data = image_info("uploaded-image")
    data["list"][0].pop("material_id")
    wire[1].extend([{"image_id": "uploaded-image"}, data])
    run(env, redis_client, task, kind="prepare", s3=env["s3"])
    run(env, redis_client, task)
    assert state(task)[0].status != "ready"
    with Session(engine) as db:
        assert not db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).all()


def test_remote_image_preview_after_cleanup_and_current_binding(
    image_env, wire, redis_client
):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.materials.catalog import remote_preview

    seed_image(image_env)
    data = image_info()
    data["list"][0]["image_url"] = (
        "https://image.example.invalid/current?signature=secret"
    )
    wire[1].append(data)
    preview = remote_preview(
        database_engine=engine,
        redis_client=redis_client,
        context=image_env["context"],
        material_id=image_env["material_id"],
    )
    assert (
        preview.media_type == "IMAGE"
        and preview.image_id == "source-image"
        and preview.video_id is None
    )
    assert "secret" not in repr(preview)
    with Session(engine) as db, db.begin():
        binding = db.exec(
            select(BCConnectionBinding).where(
                BCConnectionBinding.connection_id == image_env["connection_id"]
            )
        ).one()
        binding.revision += 1
    with pytest.raises(DomainError):
        remote_preview(
            database_engine=engine,
            redis_client=redis_client,
            context=image_env["context"],
            material_id=image_env["material_id"],
        )
    assert len(wire[0]) == 1


@pytest.mark.parametrize("change", ["binding", "access", "mapping"])
def test_image_preview_rechecks_authority_after_response(
    image_env, wire, redis_client, change
):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.materials.catalog import remote_preview

    env = image_env
    seed_image(env)

    def changed():
        with Session(engine) as db, db.begin():
            if change == "binding":
                binding = db.exec(
                    select(BCConnectionBinding).where(
                        BCConnectionBinding.connection_id == env["connection_id"]
                    )
                ).one()
                binding.revision += 1
            elif change == "access":
                db.get(
                    BCAccountAccess,
                    (
                        env["context"].tenant_id,
                        env["bc_id"],
                        "actual-account",
                        env["connection_id"],
                    ),
                ).authorized = False
            else:
                asset = db.exec(
                    select(AccountMaterial).where(
                        AccountMaterial.material_id == env["material_id"]
                    )
                ).one()
                asset.image_id = "replacement-image"
        data = image_info()
        data["list"][0]["image_url"] = (
            "https://image.example.invalid/fresh?signature=secret"
        )
        return data

    wire[1].append(changed)
    with pytest.raises(DomainError):
        remote_preview(
            database_engine=engine,
            redis_client=redis_client,
            context=env["context"],
            material_id=env["material_id"],
        )
    assert len(wire[0]) == 1


def test_readiness_skips_revoked_source_for_another_legal_source(image_env):
    from app.modules.accounts.models import BCAccountAccess
    from tests.modules.materials.test_readiness import read

    env = image_env
    seed_image(env)
    seed_image(env, "target-two")
    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                env["context"].tenant_id,
                env["bc_id"],
                "actual-account",
                env["connection_id"],
            ),
        ).authorized = False
    result = read(env, "target-account")
    assert result.state == "preparable" and result.path == "share_source"
    task = queue_images(env).items[0].task_id
    assert state(task)[1].remote_response["source_advertiser_id"] == "target-two"


def test_cross_bc_original_is_blocked_before_any_dispatch(image_env):
    from app.jobs.models import PendingDispatch
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess, TenantBC
    from app.modules.materials.readiness import get_material_readiness

    env = image_env
    with Session(engine) as db, db.begin():
        db.add(TenantBC(tenant_id=env["context"].tenant_id, bc_id="target-bc"))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=env["context"].tenant_id,
                bc_id="target-bc",
                connection_id=env["connection_id"],
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        db.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id="target-bc",
                advertiser_id="target-account",
                connection_id=env["connection_id"],
                in_bc=True,
                authorized=True,
                active=True,
                can_upload=True,
                can_build=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            )
        )
        db.flush()
        route = freeze_route(
            db,
            context=env["context"],
            bc_id="target-bc",
            connection_id=env["connection_id"],
        )
        result = get_material_readiness(
            db,
            context=env["context"],
            bc_id="target-bc",
            material_id=env["material_id"],
            advertiser_id="target-account",
            route=route,
        )
        assert (
            result.state == "blocked"
            and result.reason_code == "image_source_unavailable"
        )
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == env["context"].tenant_id
            )
        ).all()


def test_shared_target_changed_id_uses_platform_signature_then_exact_readback(
    image_env, wire, redis_client
):
    seed_image(image_env)
    task = queue_images(image_env).items[0].task_id
    source = image_info()
    source["list"][0]["signature"] = "f" * 32
    candidate = image_info("target-image")
    candidate["list"][0].update(signature="f" * 32, width=540, height=960)
    page = {
        **candidate,
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }
    wire[1].extend([source, {}, {"list": []}, page, candidate])
    run(image_env, redis_client, task, kind="prepare")
    run(image_env, redis_client, task)
    run(image_env, redis_client, task)
    assert state(task)[2] is None
    run(image_env, redis_client, task)
    assert state(task)[2].image_id == "target-image"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


@pytest.mark.parametrize(
    "code",
    [
        "image_source_unavailable",
        "image_preview_unavailable",
        "image_not_carousel_usable",
        "image_share_failed",
        "image_reconciliation_ambiguous",
        "image_source_frozen",
        "image_material_required",
        "image_distribution_limit",
        "image_pipeline_required",
    ],
)
def test_image_distribution_errors_are_actionable(code):
    from app.core.errors import ERROR_HTTP_STATUS
    from app.modules.materials.uploads import public_error

    assert public_error(code) == code
    assert ERROR_HTTP_STATUS[code] in {409, 422}
