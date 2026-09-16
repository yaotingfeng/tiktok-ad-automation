"""租户共享内容使用实际 BC 的封面与来源，真实 PG/Redis，仅替换 SDK HTTP。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from urllib3.exceptions import ReadTimeoutError

from app.core.config import settings
from app.core.db import engine
from app.core.errors import DomainError
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.accounts.routing import freeze_route
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialFile,
    ObjectUpload,
)
from app.modules.materials.remote_sources import read_remote_source
from tests.modules.materials.test_cover_throughput import drive, enqueue, image, page
from tests.modules.materials.test_covers import job_state, run, scopes, video_info
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_source_uploads import CONTENT, MD5
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire

PRIMARY = "primary-b"
TARGET = "target-b"
PREVIEW = "https://media.vetted.example/temporary-source"


@pytest.fixture
def library_env(source_env, monkeypatch):
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.vetted.example"})
    )
    scopes(source_env)
    destination = {**source_env, "bc_id": "bc-target"}
    with Session(engine) as db, db.begin():
        db.add(TenantBC(tenant_id=source_env["context"].tenant_id, bc_id="bc-target"))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=source_env["context"].tenant_id,
                bc_id="bc-target",
                connection_id=source_env["connection_id"],
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=source_env["context"].tenant_id,
                bc_id="bc-target",
                connection_id=source_env["connection_id"],
            )
        )
        asset(db, source_env, "actual-account")
        for account in (PRIMARY, TARGET):
            target(db, destination, advertiser_id=account)
            mapping = asset(db, destination, account)
            if account == PRIMARY:
                primary_id = mapping.id
    return {**destination, "source_env": source_env, "primary_id": primary_id}


def owned_relay(env, *, transport="url_relay", status="succeeded", receipt=None):
    with Session(engine) as db, db.begin():
        operation = MaterialAssetOperation(
            tenant_id=env["context"].tenant_id,
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id=PRIMARY,
            path="share_source",
            status=status,
            request_digest="a" * 64,
            frozen_route=freeze_route(
                db,
                context=env["context"],
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
            ).model_dump(mode="json"),
            remote_response={
                "video_id": f"vid-{PRIMARY}",
                "upload_video_id": receipt or f"vid-{PRIMARY}",
                "transport": transport,
                "content_md5": MD5,
            },
        )
        db.add(operation)
        db.flush()
        return operation.id


def primary_video():
    result = video_info()
    result["list"][0]["video_id"] = f"vid-{PRIMARY}"
    return result


def preview_info():
    return {
        "list": [
            {
                "video_id": f"vid-{PRIMARY}",
                "signature": MD5,
                "displayable": True,
                "width": 720,
                "height": 1280,
                "duration": 4.5,
                "size": len(CONTENT),
                "format": "mp4",
                "preview_url": PREVIEW,
            }
        ]
    }


def read_primary(env, redis_client):
    return read_remote_source(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        bc_id=env["bc_id"],
        material_id=env["material_id"],
        source_asset_id=env["primary_id"],
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def alias_target(env, account):
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, env["material_id"])
        original.sha256 = sha256(CONTENT).hexdigest()
        original.digest_verified_at = datetime.now(UTC)
        alias_id = uuid4()
        duplicate = MaterialFile(
            **{
                **original.model_dump(),
                "id": alias_id,
                "object_key": f"alias/{alias_id}",
                "file_name": "alias.mp4",
            }
        )
        db.add(duplicate)
        db.flush()
        env = {**env, "material_id": alias_id}
        asset(db, env, account)
    return env


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("uncertain", [False, True])
def test_relay_primary_cover_upload_then_local_image_share_preserves_origin(
    library_env, redis_client, wire, uncertain, alias
):
    env = library_env
    operation_id = owned_relay(env)
    if alias:
        env = alias_target(env, TARGET)
    identity = enqueue(env, account=TARGET).task_id
    run(env, redis_client, identity)
    with Session(engine) as db:
        primary_job = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == env["context"].tenant_id,
                MaterialCoverJob.advertiser_id == PRIMARY,
            )
        ).one()
        assert primary_job.purpose == "SOURCE"
        primary_identity = primary_job.id
    assert not wire[0]
    wire[1].extend([primary_video(), image(0)])
    run(env, redis_client, primary_identity)
    wire[1].append({"list": [image(0)]})
    run(env, redis_client, primary_identity, read=True)
    assert job_state(primary_identity).status == "READY"
    wire[1].extend(
        [
            {"list": [image(0)]},
            page([]),
            ReadTimeoutError(None, "offline", "timeout")
            if uncertain
            else {"failed_infos": {}},
        ]
    )
    drive(env, redis_client, identity)
    if uncertain:
        with Session(engine) as db:
            batch = db.get(MaterialCoverShareBatch, job_state(identity).share_batch_id)
            assert batch.status == "UNKNOWN" and batch.armed_at is not None
    wire[1].append(page([image(0, target_id=True)]))
    drive(env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    posts = [call for call in wire[0] if call[0] == "POST"]
    assert len(posts) == 2
    assert json.loads(posts[0][2]["body"])["advertiser_id"] == PRIMARY
    assert json.loads(posts[1][2]["body"]) == {
        "advertiser_id": PRIMARY,
        "asset_type": "IMAGE",
        "material_ids": ["900000"],
        "shared_advertiser_ids": [TARGET],
    }
    with Session(engine) as db:
        material = db.get(MaterialFile, env["material_id"])
        operation = db.get(MaterialAssetOperation, operation_id)
        mapping = db.get(AccountMaterial, job_state(identity).asset_id)
        assert material.bc_id == "bc-fixture"
        assert (operation.bc_id, operation.advertiser_id) == ("bc-target", PRIMARY)
        assert (mapping.bc_id, mapping.video_id, mapping.image_id) == (
            "bc-target",
            f"vid-{TARGET}",
            "tos-target-0",
        )
    assert not wire[1]


@pytest.mark.parametrize("present", [True, False])
def test_primary_alias_reuses_its_owned_cover_without_self_share(
    library_env, redis_client, wire, present
):
    owned_relay(library_env)
    env = alias_target(library_env, PRIMARY)
    identity = enqueue(env, account=PRIMARY).task_id
    run(env, redis_client, identity)
    with Session(engine) as db:
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == env["context"].tenant_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        assert source.material_id == library_env["material_id"]
        assert source.id != identity
        source_identity = source.id
    wire[1].extend([primary_video(), image(0)])
    run(env, redis_client, source_identity)
    wire[1].append({"list": [image(0)]})
    run(env, redis_client, source_identity, read=True)
    wire[1].extend([{"list": [image(0)]}, page([image(0)] if present else [])])
    drive(env, redis_client, identity)
    assert job_state(identity).status == ("READY" if present else "BLOCKED")
    assert job_state(identity).purpose == "BUILD"
    posts = [call for call in wire[0] if call[0] == "POST"]
    assert len(posts) == 1
    assert "image/ad/upload" in posts[0][1]
    assert not wire[1]


def test_original_bc_ready_image_cannot_be_shared_directly_into_target_bc(
    library_env, redis_client, wire
):
    origin = library_env["source_env"]
    source_identity = enqueue(origin, source=True).task_id
    with Session(engine) as db, db.begin():
        job = db.get(MaterialCoverJob, source_identity)
        job.status = "READY"
        job.request_armed_at = datetime.now(UTC)
        job.known_image_id, job.image_mid = "tos-source-0", "900000"
        job.signature, job.width, job.height = image(0)["signature"], 720, 1280
        db.get(AccountMaterial, job.asset_id).image_id = "tos-source-0"
    identity = enqueue(library_env, account=TARGET).task_id
    run(library_env, redis_client, identity)
    assert job_state(identity).status == "BLOCKED"
    assert job_state(identity).error_code == "cover_source_unavailable"
    assert not wire[0]


@pytest.mark.parametrize(
    "transport,status,receipt",
    [
        ("native_share", "succeeded", None),
        ("url_relay", "result_unknown", None),
        ("url_relay", "succeeded", "conflicting-vid"),
    ],
)
@pytest.mark.parametrize("alias", [False, True])
def test_shared_or_uncertain_video_never_becomes_primary_cover_owner(
    library_env, redis_client, wire, transport, status, receipt, alias
):
    owned_relay(library_env, transport=transport, status=status, receipt=receipt)
    env = alias_target(library_env, TARGET) if alias else library_env
    identity = enqueue(env, account=TARGET).task_id
    run(env, redis_client, identity)
    assert job_state(identity).status == "BLOCKED"
    with Session(engine) as db:
        assert not db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == library_env["context"].tenant_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).all()
    assert not wire[0]


@pytest.mark.parametrize("changed", ["sha256", "byte_size", "digest_verified_at"])
def test_alias_source_identity_changed_during_read_never_shares_image(
    library_env, redis_client, wire, changed
):
    owned_relay(library_env)
    env = alias_target(library_env, TARGET)
    source_identity = enqueue(library_env, account=PRIMARY, source=True).task_id
    with Session(engine) as db, db.begin():
        job = db.get(MaterialCoverJob, source_identity)
        job.status = "READY"
        job.request_armed_at = datetime.now(UTC)
        job.known_image_id, job.image_mid = "tos-source-0", "900000"
        job.signature, job.width, job.height = image(0)["signature"], 720, 1280
        db.get(AccountMaterial, job.asset_id).image_id = "tos-source-0"
    identity = enqueue(env, account=TARGET).task_id

    def changed_response():
        with Session(engine) as db, db.begin():
            original = db.get(MaterialFile, library_env["material_id"])
            setattr(
                original,
                changed,
                {
                    "sha256": "f" * 64,
                    "byte_size": len(CONTENT) + 1,
                    "digest_verified_at": None,
                }[changed],
            )
        return {"list": [image(0)]}

    wire[1].append(changed_response)
    drive(env, redis_client, identity)
    assert job_state(identity).status == "BLOCKED"
    assert job_state(identity).error_code == "cover_source_changed"
    assert len(wire[0]) == 1 and wire[0][0][0] == "GET"
    assert not wire[1]


def test_remote_preview_uses_actual_bc_without_changing_original_bc(
    library_env, redis_client, wire
):
    wire[1].append(preview_info())
    preview = read_primary(library_env, redis_client)
    assert (preview.advertiser_id, preview.video_id, preview.url) == (
        PRIMARY,
        f"vid-{PRIMARY}",
        PREVIEW,
    )
    assert len(wire[0]) == 1
    assert dict(wire[0][0][2]["fields"])["advertiser_id"] == PRIMARY
    with Session(engine) as db:
        assert db.get(MaterialFile, library_env["material_id"]).bc_id == "bc-fixture"


@pytest.mark.parametrize("change", ["provenance", "digest", "authorization"])
def test_remote_preview_rechecks_shared_identity_and_source_grant_after_http(
    library_env, redis_client, wire, change
):
    if change == "provenance":
        # 无上传对象关联的历史素材同样要保护来源快照，不能只依赖原件 FK。
        with Session(engine) as db, db.begin():
            row = db.exec(
                select(ObjectUpload).where(
                    ObjectUpload.material_id == library_env["material_id"]
                )
            ).one()
            db.delete(row)

    def changed_response():
        with Session(engine) as db, db.begin():
            if change == "provenance":
                db.get(MaterialFile, library_env["material_id"]).bc_id = "bc-target"
            elif change == "digest":
                db.get(MaterialFile, library_env["material_id"]).video_md5 = "f" * 32
            else:
                grant = db.get(
                    BCAccountAccess,
                    (
                        library_env["context"].tenant_id,
                        library_env["bc_id"],
                        PRIMARY,
                        library_env["connection_id"],
                    ),
                )
                grant.authorized = False
        return preview_info()

    wire[1].append(changed_response)
    with pytest.raises(DomainError):
        read_primary(library_env, redis_client)
    assert len(wire[0]) == 1
