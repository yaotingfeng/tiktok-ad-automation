"""真实gateway、PG/Redis与本地HTTP封面闭环；只创建测试自有素材事实。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.modules.accounts.models import TikTokConnection
from app.modules.materials import covers
from app.modules.materials.cover_models import (
    MaterialCoverJob,
    MaterialCoverJobPage,
    MaterialCoverReceipt,
)
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy


@pytest.fixture
def cover_env(gateway_case, gateway_wire, database_engine, monkeypatch):
    assert gateway_wire["wire"].calls == []
    context, route, advertiser = gateway_case
    with Session(database_engine) as db, db.begin():
        if route.channel == "OFFICIAL_API":
            connection = db.get(TikTokConnection, route.connection_id)
            private = decrypt_credentials(
                tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
            )
            private["scope"] = "[6]"
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=context.tenant_id, value=private
            )
            connection.credential_revision += 1
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name="cover-fixture.mp4",
            object_key=f"offline/{uuid4()}",
            byte_size=120,
            video_md5="a" * 32,
            sha256="b" * 64,
            storage_state="unavailable",
        )
        db.add(material)
        db.flush()
        asset = AccountMaterial(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            material_id=material.id,
            advertiser_id=advertiser,
            connection_id=route.connection_id,
            video_id="actual-target-vid",
            status="available",
            verified_at=datetime.now(UTC),
        )
        db.add(asset)
        db.flush()
        env = {
            "context": context,
            "route": route,
            "advertiser": advertiser,
            "material_id": material.id,
            "asset_id": asset.id,
        }
    if route.channel == "OFFICIAL_MCP":
        monkeypatch.setattr(settings, "TIKTOK_APP_ID", "")
        monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "")
    try:
        yield env
    finally:
        with Session(database_engine) as db, db.begin():
            for model in (
                MaterialCoverJobPage,
                MaterialCoverReceipt,
                MaterialCoverJob,
                AccountMaterial,
                MaterialFile,
            ):
                db.execute(delete(model).where(model.tenant_id == context.tenant_id))


def queue(env, database_engine):
    with Session(database_engine) as db, db.begin():
        return covers.ensure_cover(
            db,
            context=env["context"],
            bc_id=env["route"].bc_id,
            material_id=env["material_id"],
            advertiser_id=env["advertiser"],
            task_key=f"cover:{uuid4()}",
            route=env["route"],
        )


def job(database_engine, identity):
    with Session(database_engine) as db:
        row = db.get(MaterialCoverJob, identity)
        db.expunge(row)
        return row


def run(env, database_engine, redis_client, identity, *, read=False):
    row = job(database_engine, identity)
    covers.run_cover(
        database_engine=database_engine,
        redis_client=redis_client,
        context=env["context"],
        job_id=identity,
        dispatch_id=row.dispatch_id,
        revision=row.revision,
        read=read,
    )


def video_data(**changes):
    return {
        "list": [
            {
                "video_id": "actual-target-vid",
                "signature": "a" * 32,
                "displayable": True,
                "width": 720,
                "height": 1280,
                "video_cover_url": "https://cdn.example/actual-cover?secret=private",
                **changes,
            }
        ]
    }


def image_data(name, **changes):
    return {
        "list": [
            {
                "image_id": "actual-image-id",
                "signature": "c" * 32,
                "displayable": True,
                "file_name": name,
                "width": 360,
                "height": 640,
                **changes,
            }
        ]
    }


def enqueue(wire, operation, data):
    wire["wire"].results[operation].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": data,
                "request_id": "cover-request",
            },
        }
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_frozen_target_cover_roundtrip_uses_actual_factory(
    cover_env, gateway_wire, database_engine, redis_client
):
    identity = queue(cover_env, database_engine).task_id
    original = job(database_engine, identity)
    assert original.frozen_route == cover_env["route"].model_dump(mode="json")
    assert original.video_id == "actual-target-vid" and original.known_image_id is None
    response = {"image_id": "actual-image-id", "signature": "c" * 32}
    sdk_responses = [video_data(), response, image_data(original.remote_name)]
    if cover_env["route"].channel == "OFFICIAL_API":
        gateway_wire["before"]["callback"] = lambda: gateway_wire["sdk_data"].update(
            data=sdk_responses.pop(0)
        )
    enqueue(gateway_wire, "file_video_ad_info_get", video_data())
    enqueue(gateway_wire, "file_image_ad_upload", response)
    enqueue(gateway_wire, "file_image_ad_info_get", image_data(original.remote_name))
    run(cover_env, database_engine, redis_client, identity)
    assert job(database_engine, identity).known_image_id == "actual-image-id"
    assert job(database_engine, identity).status == "VERIFYING"
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "READY"
    with Session(database_engine) as db:
        assert (
            db.get(AccountMaterial, cover_env["asset_id"]).image_id == "actual-image-id"
        )
    actual = (
        gateway_wire["sdk_calls"]
        if cover_env["route"].channel == "OFFICIAL_API"
        else [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    )
    assert len(actual) == 3


def prepare_replies(env, wire, steps):
    values = [data for _, data in steps]
    if env["route"].channel == "OFFICIAL_API":
        wire["before"]["callback"] = lambda: wire["sdk_data"].update(data=values.pop(0))
    for tool, data in steps:
        enqueue(wire, tool, data)


def post_count(env, wire):
    if env["route"].channel == "OFFICIAL_API":
        return sum(c[0] == "POST" for c in wire["sdk_calls"])
    return sum(
        c["method"] == "tools/call" and c["params"]["name"] == "file_image_ad_upload"
        for c in wire["wire"].calls
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change",
    [
        {"displayable": False},
        {"signature": "d" * 32},
        {"width": 640, "height": 360},
        {"image_id": "another-image"},
    ],
)
def test_unusable_image_detail_preserves_known_id_without_publishing(
    cover_env, gateway_wire, database_engine, redis_client, change
):
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
            (
                "file_image_ad_info_get",
                image_data(job(database_engine, identity).remote_name, **change),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    run(cover_env, database_engine, redis_client, identity, read=True)
    current = job(database_engine, identity)
    assert current.known_image_id == "actual-image-id" and current.status == "UNKNOWN"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_receipt_is_saved_before_actual_client_cleanup_failure(
    cover_env, gateway_wire, database_engine, redis_client, monkeypatch
):
    from urllib3 import PoolManager

    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
        ],
    )
    observed = []
    if cover_env["route"].channel == "OFFICIAL_API":
        original = PoolManager.clear

        def cleanup(pool):
            original(pool)
            if post_count(cover_env, gateway_wire):
                observed.append(job(database_engine, identity).known_image_id)
                raise RuntimeError("synthetic cleanup failure")

        monkeypatch.setattr(PoolManager, "clear", cleanup)
    else:
        import httpx2

        original_close = httpx2.AsyncHTTPTransport.aclose

        async def cleanup_transport(transport):
            await original_close(transport)
            if post_count(cover_env, gateway_wire):
                observed.append(job(database_engine, identity).known_image_id)
                raise RuntimeError("synthetic transport cleanup failure")

        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "aclose", cleanup_transport)
    run(cover_env, database_engine, redis_client, identity)
    assert observed and all(value == "actual-image-id" for value in observed)
    current = job(database_engine, identity)
    assert current.known_image_id == "actual-image-id" and current.status == "VERIFYING"
    run(cover_env, database_engine, redis_client, identity)
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("mutation", ["video", "digest", "read", "upload", "claim"])
def test_target_or_authority_changed_between_reads_and_upload_sends_no_image(
    cover_env, gateway_wire, database_engine, redis_client, mutation
):
    from app.modules.accounts.connection_models import ConnectionAuthorization

    identity = queue(cover_env, database_engine).task_id
    replacement = uuid4()
    changed = []

    def mutate():
        if changed:
            return
        if cover_env["route"].channel == "OFFICIAL_MCP" and not any(
            c["method"] == "tools/call" for c in gateway_wire["wire"].calls
        ):
            return
        with Session(database_engine) as db, db.begin():
            if mutation == "video":
                db.get(
                    AccountMaterial, cover_env["asset_id"]
                ).video_id = "replacement-video"
            elif mutation == "digest":
                db.get(MaterialFile, cover_env["material_id"]).video_md5 = "b" * 32
            elif mutation == "claim":
                db.get(MaterialCoverJob, identity).claim_token = replacement
            else:
                facts = db.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id
                        == cover_env["route"].connection_id
                    )
                ).one()
                facts.permission_summary = {
                    **facts.permission_summary,
                    f"{mutation}_authorized": False,
                }
        changed.append(True)

    def api_response():
        gateway_wire["sdk_data"]["data"] = video_data()
        mutate()

    gateway_wire["before"]["callback"] = (
        api_response if cover_env["route"].channel == "OFFICIAL_API" else mutate
    )
    enqueue(gateway_wire, "file_video_ad_info_get", video_data())
    run(cover_env, database_engine, redis_client, identity)
    assert changed and post_count(cover_env, gateway_wire) == 0
    current = job(database_engine, identity)
    assert current.known_image_id is None
    if mutation == "claim":
        assert current.claim_token == replacement
    else:
        assert current.status == "BLOCKED"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_unknown_upload_with_two_search_candidates_never_reposts_or_publishes(
    cover_env, gateway_wire, database_engine, redis_client, monkeypatch
):
    from urllib3 import PoolManager
    from urllib3.exceptions import ReadTimeoutError

    identity = queue(cover_env, database_engine).task_id
    name = job(database_engine, identity).remote_name
    search = {
        "list": [
            {"image_id": "image-1", "file_name": name},
            {"image_id": "image-2", "file_name": name},
        ],
        "page_info": {"page": 1, "page_size": 100, "total_number": 2, "total_page": 1},
    }
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            ("file_image_ad_upload", {}),
            ("file_image_ad_search", search),
        ],
    )
    if cover_env["route"].channel == "OFFICIAL_API":
        original = PoolManager.request

        def disconnect(pool, method, url, **kwargs):
            result = original(pool, method, url, **kwargs)
            if method == "POST":
                raise ReadTimeoutError(None, url, "synthetic accepted disconnect")
            return result

        monkeypatch.setattr(PoolManager, "request", disconnect)
    else:
        gateway_wire["wire"].disconnect_after_accept("file_image_ad_upload")
    run(cover_env, database_engine, redis_client, identity)
    assert job(database_engine, identity).request_armed_at is not None
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert (
        job(database_engine, identity).status == "UNKNOWN"
        and job(database_engine, identity).known_image_id is None
    )
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("matching", [True, False])
def test_only_same_ratio_target_suggestion_can_be_uploaded(
    cover_env, gateway_wire, database_engine, redis_client, matching
):
    import json

    identity = queue(cover_env, database_engine).task_id
    rows = [
        {
            "id": "unusable-suggestion",
            "url": "https://cdn.example/wrong",
            "width": 640,
            "height": 360,
        }
    ]
    if matching:
        rows.append(
            {
                "id": "suggestion-not-image-id",
                "url": "https://cdn.example/right",
                "width": 360,
                "height": 640,
            }
        )
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data(video_cover_url=None)),
            ("file_video_suggestcover_get", {"list": rows}),
            ("file_image_ad_upload", {"image_id": "actual-image-id"}),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert post_count(cover_env, gateway_wire) == int(matching)
    if not matching:
        assert current.status == "BLOCKED" and current.known_image_id is None
        return
    assert current.known_image_id == "actual-image-id"
    if cover_env["route"].channel == "OFFICIAL_API":
        post = next(c for c in gateway_wire["sdk_calls"] if c[0] == "POST")
        arguments = json.loads(post[2]["body"])
    else:
        post = next(
            c
            for c in gateway_wire["wire"].calls
            if c["method"] == "tools/call"
            and c["params"]["name"] == "file_image_ad_upload"
        )
        arguments = post["params"]["arguments"]
    assert arguments["image_url"] == "https://cdn.example/right"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_original_digest_changed_between_prepare_and_verify_cannot_publish(
    cover_env, gateway_wire, database_engine, redis_client
):
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
            (
                "file_image_ad_info_get",
                image_data(job(database_engine, identity).remote_name),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    with Session(database_engine) as db, db.begin():
        db.get(MaterialFile, cover_env["material_id"]).video_md5 = "b" * 32
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status != "READY"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "receipt_signature,remembered_signature,expected",
    [
        ("c" * 32, None, "UNKNOWN"),
        (None, None, "READY"),
        ("c" * 32, "d" * 32, "UNKNOWN"),
    ],
)
def test_fallback_receipt_keeps_observed_signature_for_strict_readback(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    receipt_signature,
    remembered_signature,
    expected,
):
    from datetime import timedelta

    from sqlalchemy import event

    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": receipt_signature},
            ),
            (
                "file_image_ad_info_get",
                image_data(
                    job(database_engine, identity).remote_name, signature="d" * 32
                ),
            ),
        ],
    )
    failed = []

    def reject_transition(_conn, _cursor, statement, _params, _context, _many):
        if (
            "UPDATE material_cover_job SET" in statement
            and "known_image_id=" in statement
            and not failed
        ):
            failed.append(True)
            raise RuntimeError("synthetic receipt transition failure")

    event.listen(database_engine, "before_cursor_execute", reject_transition)
    try:
        run(cover_env, database_engine, redis_client, identity)
    finally:
        event.remove(database_engine, "before_cursor_execute", reject_transition)
    assert failed
    with Session(database_engine) as db, db.begin():
        row = db.get(MaterialCoverJob, identity)
        row.signature = remembered_signature
        fallback = db.exec(
            select(MaterialCoverReceipt).where(MaterialCoverReceipt.job_id == identity)
        ).one()
        assert fallback.receipt_facts == {"signature": receipt_signature}
        row.claimed_until = row.repair_after = covers._now() - timedelta(seconds=1)
        covers.repair_cover_dispatches(db)
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == expected
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id == (
            "actual-image-id" if expected == "READY" else None
        )
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_legacy_receipt_without_observed_facts_cannot_publish(
    cover_env, gateway_wire, database_engine, redis_client
):
    identity = queue(cover_env, database_engine).task_id
    with Session(database_engine) as db, db.begin():
        row = db.get(MaterialCoverJob, identity)
        row.request_armed_at = covers._now()
        db.add(
            MaterialCoverReceipt(
                tenant_id=row.tenant_id,
                job_id=row.id,
                image_id="historical-image",
                receipt_facts=None,
            )
        )
        covers._queue(db, row, read=True)
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "UNKNOWN"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    assert post_count(cover_env, gateway_wire) == 0
    assert not gateway_wire["sdk_calls"] and not gateway_wire["wire"].calls


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_late_upload_signature_conflict_removes_ready_image_without_reupload(
    cover_env, gateway_wire, database_engine, redis_client, monkeypatch
):
    import asyncio
    import json
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event

    import httpx2
    from urllib3 import PoolManager

    from app.integrations.tiktok.contracts.materials import ImageReceipt

    identity = queue(cover_env, database_engine).task_id
    name = job(database_engine, identity).remote_name
    search = {
        "list": [{"image_id": "actual-image-id", "file_name": name}],
        "page_info": {"page": 1, "page_size": 100, "total_number": 1, "total_page": 1},
    }
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
            ("file_image_ad_search", search),
            ("file_image_ad_info_get", image_data(name, signature="d" * 32)),
        ],
    )
    accepted, release = Event(), Event()
    if cover_env["route"].channel == "OFFICIAL_API":
        original = PoolManager.request

        def delayed(pool, method, url, **kwargs):
            response = original(pool, method, url, **kwargs)
            if method == "POST":
                accepted.set()
                assert release.wait(15)
            return response

        monkeypatch.setattr(PoolManager, "request", delayed)
    else:
        original_async = httpx2.AsyncHTTPTransport.handle_async_request

        async def delayed_async(transport, request):
            response = await original_async(transport, request)
            payload = json.loads(request.content) if request.method == "POST" else {}
            if payload.get("params", {}).get("name") == "file_image_ad_upload":
                accepted.set()
                assert await asyncio.to_thread(release.wait, 15)
            return response

        monkeypatch.setattr(
            httpx2.AsyncHTTPTransport, "handle_async_request", delayed_async
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, cover_env, database_engine, redis_client, identity)
        try:
            assert accepted.wait(5)
            with Session(database_engine) as db, db.begin():
                row = db.get(MaterialCoverJob, identity)
                row.claimed_until = row.repair_after = covers._now() - timedelta(
                    seconds=1
                )
                covers.repair_cover_dispatches(db)
            run(cover_env, database_engine, redis_client, identity, read=True)
            run(cover_env, database_engine, redis_client, identity, read=True)
            assert job(database_engine, identity).status == "READY"
            assert job(database_engine, identity).signature == "d" * 32
        finally:
            release.set()
        future.result(timeout=10)
    assert job(database_engine, identity).status == "UNKNOWN"
    assert job(database_engine, identity).known_image_id == "actual-image-id"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    for signature in ("c" * 32, "b" * 32):
        covers._preserve_receipt(
            database_engine,
            cover_env["context"],
            identity,
            ImageReceipt("actual-image-id", signature),
        )
    with Session(database_engine) as db:
        receipt = db.exec(
            select(MaterialCoverReceipt).where(MaterialCoverReceipt.job_id == identity)
        ).one()
        assert receipt.receipt_facts == {"signature": "c" * 32}
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_ready_cover_cannot_be_reused_after_original_digest_changed(
    cover_env, gateway_wire, database_engine, redis_client
):
    from app.core.errors import DomainError

    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
            (
                "file_image_ad_info_get",
                image_data(job(database_engine, identity).remote_name),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "READY"
    with Session(database_engine) as db, db.begin():
        db.get(MaterialFile, cover_env["material_id"]).video_md5 = "b" * 32
    with pytest.raises(DomainError) as caught:
        queue(cover_env, database_engine)
    assert caught.value.code == "cover_video_changed"
    assert post_count(cover_env, gateway_wire) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_duplicate_receipt_conflict_survives_explicit_read_reconciliation(
    cover_env, gateway_wire, database_engine, redis_client
):
    from app.integrations.tiktok.contracts.materials import ImageReceipt

    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            (
                "file_image_ad_upload",
                {"image_id": "actual-image-id", "signature": "c" * 32},
            ),
            (
                "file_image_ad_info_get",
                image_data(job(database_engine, identity).remote_name),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    run(cover_env, database_engine, redis_client, identity, read=True)
    for signature in ("c" * 32, "d" * 32):
        covers._preserve_receipt(
            database_engine,
            cover_env["context"],
            identity,
            ImageReceipt("actual-image-id", signature),
        )
    before = len(gateway_wire["sdk_calls"]), len(gateway_wire["wire"].calls)
    with Session(database_engine) as db, db.begin():
        result = covers.request_cover_reconciliation(
            db, context=cover_env["context"], job_id=identity
        )
        assert result.state == "blocked"
    current = job(database_engine, identity)
    assert (
        current.status == "UNKNOWN" and current.error_code == "cover_receipt_ambiguous"
    )
    assert current.dispatch_id is None and post_count(cover_env, gateway_wire) == 1
    assert before == (len(gateway_wire["sdk_calls"]), len(gateway_wire["wire"].calls))


def seed_ready_status(env, database_engine, *, md5):
    with Session(database_engine) as db, db.begin():
        row = MaterialCoverJob(
            tenant_id=env["context"].tenant_id,
            bc_id=env["route"].bc_id,
            material_id=env["material_id"],
            asset_id=env["asset_id"],
            advertiser_id=env["advertiser"],
            connection_id=env["route"].connection_id,
            actor_id=env["context"].actor_id,
            frozen_route=env["route"].model_dump(mode="json"),
            video_id="actual-target-vid",
            video_md5=md5,
            remote_name="historical-cover.jpg",
            status="READY",
            known_image_id="actual-image-id",
            signature="c" * 32,
            request_armed_at=covers._now(),
            width=720,
            height=1280,
        )
        db.add(row)
        db.get(AccountMaterial, env["asset_id"]).image_id = "actual-image-id"
        return row.id


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("original_md5", [None, "b" * 32])
def test_status_get_blocks_missing_or_changed_original_digest_for_viewer(
    cover_env, gateway_wire, database_engine, original_md5
):
    from app.modules.tenants.models import TenantMembership

    identity = seed_ready_status(cover_env, database_engine, md5=original_md5)
    with Session(database_engine) as db, db.begin():
        member = db.get(
            TenantMembership,
            (cover_env["context"].tenant_id, cover_env["context"].actor_id),
        )
        member.role = "viewer"
    with Session(database_engine) as db:
        result = covers.get_cover_status(
            db, context=cover_env["context"], job_id=identity
        )
        assert result.state == "blocked"
        assert result.reason_code == (
            "cover_video_unverified" if original_md5 is None else "cover_video_changed"
        )
    assert not gateway_wire["sdk_calls"] and not gateway_wire["wire"].calls


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("revoke_original", [False, True])
def test_status_get_authorizes_frozen_route_after_default_changes(
    cover_env, gateway_wire, database_engine, revoke_original
):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
        ConnectionAuthorization,
    )
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.tenants.models import TenantMembership

    identity = seed_ready_status(cover_env, database_engine, md5="a" * 32)
    alternate = uuid4()
    with Session(database_engine) as db, db.begin():
        original = db.get(TikTokConnection, cover_env["route"].connection_id)
        db.add(TikTokConnection(**{**original.model_dump(), "id": alternate}))
        db.flush()
        for model in (BCAccountAccess, BCConnectionBinding, ConnectionAuthorization):
            original_row = db.exec(
                select(model).where(model.connection_id == original.id)
            ).one()
            data = original_row.model_dump()
            if "id" in data:
                data["id"] = uuid4()
            data["connection_id"] = alternate
            db.add(model(**data))
        db.flush()
        default = db.get(
            BCDefaultRoute, (cover_env["context"].tenant_id, cover_env["route"].bc_id)
        )
        default.connection_id = alternate
        facts = db.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == original.id
            )
        ).one()
        facts.permission_summary = {
            **facts.permission_summary,
            "read_authorized": not revoke_original,
            "build_authorized": False,
        }
        db.get(
            TenantMembership,
            (cover_env["context"].tenant_id, cover_env["context"].actor_id),
        ).role = "viewer"
    with Session(database_engine) as db:
        if revoke_original:
            with pytest.raises(DomainError):
                covers.get_cover_status(
                    db, context=cover_env["context"], job_id=identity
                )
        else:
            assert (
                covers.get_cover_status(
                    db, context=cover_env["context"], job_id=identity
                ).state
                == "ready"
            )
    assert not gateway_wire["sdk_calls"] and not gateway_wire["wire"].calls


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "case", ["fresh", "stale", "read_revoked", "wrong_route", "historical_job"]
)
def test_preverified_external_cover_without_job_does_not_require_original_digest(
    cover_env, gateway_wire, database_engine, case
):
    from datetime import timedelta

    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import ConnectionAuthorization

    if case == "historical_job":
        seed_ready_status(cover_env, database_engine, md5=None)
    with Session(database_engine) as db, db.begin():
        db.get(MaterialFile, cover_env["material_id"]).video_md5 = None
        mapping = db.get(AccountMaterial, cover_env["asset_id"])
        mapping.image_id = "actual-image-id"
        if case == "stale":
            mapping.verified_at = covers._now() - timedelta(days=7)
        if case == "read_revoked":
            facts = db.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id
                    == cover_env["route"].connection_id
                )
            ).one()
            facts.permission_summary = {
                **facts.permission_summary,
                "read_authorized": False,
            }
    route = cover_env["route"]
    if case == "wrong_route":
        route = route.model_copy(update={"connection_id": uuid4()})
    if case in {"read_revoked", "wrong_route", "historical_job"}:
        with pytest.raises(DomainError):
            queue({**cover_env, "route": route}, database_engine)
    else:
        result = queue(cover_env, database_engine)
        assert result.state == ("ready" if case == "fresh" else "blocked")
        if case == "fresh":
            assert (
                result.mapping.image_id == "actual-image-id" and result.task_id is None
            )
        else:
            assert result.reason_code == "cover_evidence_stale"
    with Session(database_engine) as db:
        jobs = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == cover_env["material_id"]
            )
        ).all()
        assert len(jobs) == int(case == "historical_job")
    assert not gateway_wire["sdk_calls"] and not gateway_wire["wire"].calls
