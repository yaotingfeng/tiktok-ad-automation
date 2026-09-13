"""实际冻结gateway、PG/Redis与URL原件状态机；仅外部HTTP/签名边界替身。"""

import json
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, SQLModel, select

from app.core.config import settings
from app.integrations.tiktok import material_upload_evidence as upload_evidence
from app.modules.materials.channel_policy import MaterialUploadPolicy
from app.modules.materials.ingest_models import IngestSessionFile, OriginalUse
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_source_uploads import CONTENT, MD5
from tests.modules.materials.test_url_ingest import (
    interrupted_publication as interrupted_publication,
)
from tests.modules.materials.test_url_ingest import operation, run
from tests.modules.materials.test_url_ingest import url_env as url_env


@pytest.fixture
def source_env(gateway_case, database_engine, monkeypatch):
    context, route, _ = gateway_case
    policies = dict(settings.TIKTOK_CALL_POLICIES)
    policies["base"] = {**policies["base"], "lease_ms": 970000}
    policies["endpoints"] = {
        "materials.upload_video_url": {"lease_ms": 970000},
        "/open_api/v1.3/file/video/ad/upload/": {"lease_ms": 970000},
    }
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policies)
    with Session(database_engine) as db, db.begin():
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name="Moon.mp4",
            object_key=f"synthetic/{uuid4()}",
            byte_size=len(CONTENT),
            sha256=sha256(CONTENT).hexdigest(),
            video_md5=MD5,
            storage_state="stored",
        )
        db.add(material)
        db.flush()
        env = {
            "context": context,
            "material_id": material.id,
            "connection_id": route.connection_id,
            "bc_id": route.bc_id,
        }
    try:
        yield env
    finally:
        with Session(database_engine) as db, db.begin():
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c and table.name.startswith(
                    (
                        "material_",
                        "account_material",
                        "object_cleanup",
                        "ingest_",
                        "source_account_",
                        "original_use",
                        "temporary_material_",
                    )
                ):
                    db.execute(
                        delete(table).where(table.c.tenant_id == context.tenant_id)
                    )


@pytest.fixture
def synthetic_contract(gateway_case, monkeypatch):
    _, route, _ = gateway_case
    record = upload_evidence.MaterialUploadEvidence(
        channel=route.channel,
        adapter_contract_revision=route.adapter_contract_revision,
        policy=MaterialUploadPolicy(1024),
        category="SYNTHETIC",
        sources=("local HTTP fixture",),
        notes="No live platform guarantee.",
    )
    monkeypatch.setattr(upload_evidence, "UPLOAD_EVIDENCE", (record,))
    return record


def enqueue_upload(gateway_case, gateway_wire):
    _, route, advertiser = gateway_case
    row = {
        "video_id": "actual-source-vid",
        "material_id": "actual-source-mid",
        "advertiser_id": advertiser,
    }
    gateway_wire["sdk_data"]["data"] = [row]
    gateway_wire["wire"].results["file_video_ad_upload"].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "request_id": "actual-receipt",
                "data": row,
            },
        }
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("native_text", [False, True])
def test_url_worker_sends_once_and_preserves_original_route_and_receipt(
    gateway_case,
    gateway_wire,
    url_env,
    redis_client,
    database_engine,
    native_text,
):
    enqueue_upload(gateway_case, gateway_wire)
    if native_text and gateway_case[1].channel == "OFFICIAL_MCP":
        response = gateway_wire["wire"].results["file_video_ad_upload"].pop()
        body = response["structuredContent"]
        body["data"] = [body["data"]]
        gateway_wire["wire"].results["file_video_ad_upload"].append(
            {"content": [{"type": "text", "text": json.dumps(body)}]}
        )
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.remote_response.get("video_id") == "actual-source-vid"
    assert op.remote_response.get("mid") == "actual-source-mid"
    assert op.frozen_route == gateway_case[1].model_dump(mode="json")
    assert op.status == "succeeded"
    assert op.remote_response["confirmation_source"] == "upload_receipt"
    run(url_env, redis_client)
    calls = [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    assert (
        len(
            gateway_wire["sdk_calls"]
            if gateway_case[1].channel == "OFFICIAL_API"
            else calls
        )
        == 1
    )
    with Session(database_engine) as db:
        use = db.exec(
            select(OriginalUse).where(OriginalUse.operation_id == op.id)
        ).one()
        assert use.released_at is not None
    assert "signature=never-store" not in str(op.remote_response)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_unknown_mcp_policy_blocks_before_signing_and_arming(
    gateway_wire, url_env, redis_client, database_engine, monkeypatch
):
    monkeypatch.setattr(upload_evidence, "UPLOAD_EVIDENCE", ())

    class NoSigning:
        def generate_presigned_url(self, *_args, **_kwargs):
            pytest.fail("未知MCP能力不得签发URL")

    run(url_env, redis_client, s3=NoSigning())
    with Session(database_engine) as db:
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == url_env["material_id"]
            )
        ).one()
        ops = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == url_env["material_id"]
            )
        ).all()
        assert (
            row.status == "blocked" and row.error_code == "material_channel_unverified"
        )
        assert all(not op.remote_response.get("send_armed") for op in ops)
    assert not [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_mcp_claim_lost_after_initialize_never_sends_business_or_releases_new_owner_use(
    gateway_case,
    gateway_wire,
    url_env,
    synthetic_contract,
    redis_client,
    database_engine,
):
    assert synthetic_contract.category == "SYNTHETIC"
    replacement = uuid4()
    changed = []

    def revoke_claim():
        if changed or not any(
            c["method"] == "initialize" for c in gateway_wire["wire"].calls
        ):
            return
        with Session(database_engine) as db, db.begin():
            op = db.exec(
                select(MaterialAssetOperation).where(
                    MaterialAssetOperation.material_id == url_env["material_id"]
                )
            ).one()
            op.attempt_token = replacement
        changed.append(True)

    gateway_wire["before"]["callback"] = revoke_claim
    enqueue_upload(gateway_case, gateway_wire)
    run(url_env, redis_client)
    assert changed and operation(url_env).attempt_token == replacement
    assert not [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    with Session(database_engine) as db:
        assert (
            db.exec(
                select(OriginalUse).where(
                    OriginalUse.operation_id == operation(url_env).id
                )
            )
            .one()
            .released_at
            is None
        )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_mcp_accepted_disconnect_is_unknown_and_never_reposts_or_releases_use(
    gateway_wire,
    url_env,
    synthetic_contract,
    redis_client,
    database_engine,
):
    assert synthetic_contract.category == "SYNTHETIC"
    gateway_wire["wire"].disconnect_after_accept("file_video_ad_upload")
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "result_unknown" and op.remote_response["send_armed"] is True
    for _ in range(2):
        run(url_env, redis_client)
    posts = [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    assert len(posts) == 1 and posts[0]["params"]["name"] == "file_video_ad_upload"
    with Session(database_engine) as db:
        assert (
            db.exec(select(OriginalUse).where(OriginalUse.operation_id == op.id))
            .one()
            .released_at
            is None
        )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_two_real_workers_share_one_mcp_upload_claim(
    gateway_case,
    gateway_wire,
    url_env,
    synthetic_contract,
    redis_client,
):
    import time
    from concurrent.futures import ThreadPoolExecutor

    assert synthetic_contract.category == "SYNTHETIC"
    enqueue_upload(gateway_case, gateway_wire)
    gateway_wire["wire"].delay = 0.6
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, url_env, redis_client)
        until = time.monotonic() + 5
        while not any(c["method"] == "tools/call" for c in gateway_wire["wire"].calls):
            assert time.monotonic() < until
            time.sleep(0.005)
        second = pool.submit(run, url_env, redis_client)
        second.result(5)
        first.result(5)
    assert operation(url_env).remote_response["video_id"] == "actual-source-vid"
    assert (
        len([c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]) == 1
    )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("revision", ["credential_revision", "authorization_revision"])
@pytest.mark.usefixtures("interrupted_publication")
def test_receipt_readback_uses_frozen_authority_and_current_credentials(
    gateway_case,
    gateway_wire,
    url_env,
    synthetic_contract,
    redis_client,
    database_engine,
    revision,
    monkeypatch,
):
    from app.modules.accounts.models import TikTokConnection
    from app.modules.materials.models import AccountMaterial
    from tests.modules.materials.test_url_ingest import info

    assert synthetic_contract.category == "SYNTHETIC"
    # MCP通道无App配置也能独立完成本地协议回归。
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "")
    enqueue_upload(gateway_case, gateway_wire)
    run(url_env, redis_client)
    op = operation(url_env)
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, gateway_case[1].connection_id)
        setattr(connection, revision, getattr(connection, revision) + 1)
        if revision == "credential_revision":
            from app.core.credentials import decrypt_credentials, encrypt_credentials

            material = decrypt_credentials(
                tenant_id=gateway_case[0].tenant_id,
                ciphertext=connection.credential_ciphertext,
            )
            material["access_token"] = "synthetic-rotated-upload-token"
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=gateway_case[0].tenant_id, value=material
            )
    gateway_wire["wire"].results["file_video_ad_info_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": info()}}
    )
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    with Session(database_engine) as db:
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == url_env["material_id"]
            )
        ).first()
    if revision == "credential_revision":
        assert mapping is not None and mapping.video_id == "actual-source-vid"
        assert operation(url_env).status == "succeeded"
        assert gateway_wire["tokens"][-1] == "Bearer synthetic-rotated-upload-token"
    else:
        assert (
            mapping is None
            and operation(url_env).remote_response["video_id"] == "actual-source-vid"
        )
    calls = [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    assert len(calls) == (2 if revision == "credential_revision" else 1)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("path", ["known", "file", "relay"])
def test_mcp_readiness_uses_same_channel_gate_without_requiring_api_app(
    gateway_case,
    source_env,
    gateway_wire,
    database_engine,
    monkeypatch,
    path,
):
    from app.jobs.models import PendingDispatch
    from app.modules.materials.readiness import get_material_readiness
    from tests.modules.materials.test_readiness import asset, target

    monkeypatch.setattr(upload_evidence, "UPLOAD_EVIDENCE", ())
    monkeypatch.setattr(settings, "TIKTOK_APP_ID", "")
    monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "")
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"approved-cdn.example"})
    )
    with Session(database_engine) as db, db.begin():
        if path == "known":
            asset(db, source_env, gateway_case[2], seconds_old=1000)
        elif path == "relay":
            other = target(db, source_env, advertiser_id="other-source")
            asset(db, source_env, other)
        result = get_material_readiness(
            db,
            context=gateway_case[0],
            bc_id=gateway_case[1].bc_id,
            material_id=source_env["material_id"],
            advertiser_id=gateway_case[2],
        )
        assert not db.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == gateway_case[0].tenant_id
            )
        ).all()
    if path == "known":
        assert result.state == "preparable" and result.path == "existing_target"
    elif path == "relay":
        assert result.state == "preparable" and result.path == "share_source"
    else:
        assert (
            result.state == "blocked"
            and result.reason_code == "material_channel_unverified"
        )
    assert gateway_wire["wire"].calls == [] and gateway_wire["sdk_calls"] == []
