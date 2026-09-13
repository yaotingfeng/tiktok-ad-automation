import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.config import settings
from app.core.db import engine
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials.models import MaterialFile
from app.modules.materials.sharing import distribution_transport
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, read, target
from tests.modules.materials.test_source_uploads import (  # noqa: F401
    MD5,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)
from tests.modules.materials.test_source_uploads import (
    wire as wire,
)

NAME = "01-The General's Wrath-CL6-JUNBO-LH-1.mp4"


@pytest.fixture
def native_env(source_env, monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset())
    with Session(engine) as db, db.begin():
        advertiser = target(db, source_env)
        asset(db, source_env, "actual-account")
        material = db.get(MaterialFile, source_env["material_id"])
        material.storage_state = "unavailable"
        material.file_name = NAME
    return {**source_env, "target": advertiser}


def source_info():
    return {
        "list": [
            {
                "video_id": "vid-actual-account",
                "material_id": "1234567890123456789",
                "file_name": NAME,
                "signature": MD5,
                "displayable": True,
            }
        ]
    }


def search_info():
    return {
        "list": [
            {
                "video_id": "actual-target-vid",
                "material_id": "target-mid",
                "file_name": NAME,
                "signature": MD5,
                "displayable": True,
            }
        ],
        "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1},
    }


def test_transport_uses_bc_not_channel():
    assert distribution_transport(source_bc_id="a", target_bc_id="a") == "native_share"
    assert distribution_transport(source_bc_id="a", target_bc_id="b") == "url_relay"


def test_same_bc_shares_without_original_or_preview_url(native_env, redis_client, wire):
    ready = read(native_env, native_env["target"])
    assert (ready.state, ready.path) == ("preparable", "share_source")
    prepared = queue(native_env, native_env["target"])
    wire[1].extend([source_info(), {}])
    run(native_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "verifying" and mapping is None
    assert op.remote_response["transport"] == "native_share"
    body = json.loads(wire[0][1][2]["body"])
    assert body == {
        "advertiser_id": "actual-account",
        "asset_type": "VIDEO",
        "material_ids": ["1234567890123456789"],
        "shared_advertiser_ids": [native_env["target"]],
    }
    assert all("/upload/" not in call[1] for call in wire[0])
    wire[1].append(search_info())
    run(native_env, redis_client, prepared.task_id)
    wire[1].append({"list": search_info()["list"]})
    run(native_env, redis_client, prepared.task_id)
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "ready" and mapping.video_id == "actual-target-vid"
    assert op.remote_response["remote_name"] == NAME


def test_unknown_share_never_reuploads_or_reshares(native_env, redis_client, wire):
    prepared = queue(native_env, native_env["target"])
    wire[1].extend([source_info(), TimeoutError("synthetic loss after send")])
    run(native_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, _ = state(prepared.task_id)
    assert dist.status == "result_unknown"
    run(native_env, redis_client, prepared.task_id, kind="prepare")
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
    run(native_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[0].status == "result_unknown"
    assert len([call for call in wire[0] if call[0] == "POST"]) == 1
    assert all("/upload/" not in call[1] for call in wire[0])


def test_source_write_permission_revoked_before_send_blocks(
    native_env, redis_client, wire
):
    prepared = queue(native_env, native_env["target"])
    with Session(engine) as db, db.begin():
        grant = db.get(
            BCAccountAccess,
            (
                native_env["context"].tenant_id,
                native_env["bc_id"],
                "actual-account",
                native_env["connection_id"],
            ),
        )
        grant.can_upload = False
    run(native_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "blocked"
    assert not wire[0]


@pytest.fixture
def cross_env(source_env, monkeypatch):
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TenantBC

    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.vetted.example"})
    )
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, source_env["material_id"])
        original.digest_verified_at = datetime.now(UTC)
        original.storage_state = "unavailable"
        account = target(db, source_env)
        bc = "other-bc"
        db.add(TenantBC(tenant_id=original.tenant_id, bc_id=bc))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=original.tenant_id,
                bc_id=bc,
                connection_id=source_env["connection_id"],
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=original.tenant_id,
                bc_id=bc,
                connection_id=source_env["connection_id"],
            )
        )
        copy = MaterialFile(
            **(
                original.model_dump()
                | {"id": uuid4(), "bc_id": bc, "object_key": str(uuid4())}
            )
        )
        db.add(copy)
        db.flush()
        source = {**source_env, "bc_id": bc, "material_id": copy.id}
        target(db, source, advertiser_id="cross-account")
        asset(db, source, "cross-account")
    return {**source_env, "target": account, "source": source}


def test_cross_bc_uses_url_and_separately_freezes_source(cross_env, redis_client, wire):
    from tests.modules.materials.test_remote_only_distribution import PREVIEW, info

    prepared = queue(cross_env, cross_env["target"])
    dist, op, _ = state(prepared.task_id)
    assert dist.source_route["bc_id"] == "other-bc"
    assert dist.target_route["bc_id"] == cross_env["bc_id"]
    assert op.remote_response["transport"] == "url_relay"
    wire[1].extend([info(vid="vid-cross-account"), [{"video_id": "cross-target"}]])
    run(cross_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "verifying"
    fields = dict(wire[0][1][2]["fields"])
    assert fields["video_url"] == PREVIEW and fields["upload_type"] == "UPLOAD_BY_URL"
    assert fields["file_name"].startswith("Moon-")
    assert all("/share/" not in call[1] for call in wire[0])
    wire[1].append(info(vid="cross-target"))
    run(cross_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[2].video_id == "cross-target"


def test_cross_bc_requires_trusted_identical_content(cross_env, wire):
    with Session(engine) as db, db.begin():
        source = db.get(MaterialFile, cross_env["source"]["material_id"])
        source.sha256 = "b" * 64
    assert read(cross_env, cross_env["target"]).state == "blocked"
    assert not wire[0]
