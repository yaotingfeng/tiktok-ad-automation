"""真实 PostgreSQL/Redis，来源和 TikTok HTTP 使用离线传输替身。"""

import io
import json
import time
from datetime import UTC, datetime, timedelta
from hashlib import md5, sha256
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.deps import get_db
from app.core.config import settings
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.main import app
from app.modules.accounts.models import TenantBC
from app.modules.materials.ingest_models import (
    ObjectCleanup,
    TemporaryMaterialObject,
)
from app.modules.materials.models import MaterialFile
from app.modules.materials.push_auth import signature
from app.modules.materials.push_models import (
    MaterialPushBatch,
    MaterialPushItem,
    PushedMaterial,
)
from app.modules.materials.push_worker import process_item, repair_imports
from app.modules.materials.source_uploads import run_source_upload
from app.modules.tenants.models import Tenant
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire

PATH = "/api/integrations/materials/batches"
SECRET = "offline-push-secret-with-more-than-32-characters"
URL = "https://materials.example.test/video.mp4?signature=offline-sensitive-url"


@pytest.fixture
def client(push_env):
    assert push_env["name"]
    previous = app.dependency_overrides.copy()

    def database():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as value:
            yield value
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def push_env(source_env, monkeypatch):
    monkeypatch.setattr(settings, "MATERIAL_INGEST_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "MATERIAL_PUSH_CLIENTS",
        {
            "test-tool": {
                "secret": SECRET,
                "actor_id": str(source_env["context"].actor_id),
            }
        },
    )
    with Session(engine) as db:
        name = db.get(Tenant, source_env["context"].tenant_id).name
    yield {**source_env, "name": name}
    with Session(engine) as db, db.begin():
        db.get(Tenant, source_env["context"].tenant_id).default_bc_id = None


def payload(env, ids=("video-1", "video-2")):
    return {
        "tenant_name": env["name"],
        "materials": [
            {"material_id": identity, "file_name": f"原名_{identity}.mp4", "url": URL}
            for identity in ids
        ],
    }


def signed(body=b"", *, method="POST", path=PATH, request_id=None, timestamp=None):
    request_id = str(request_id or uuid4())
    timestamp = str(timestamp or int(time.time()))
    return {
        "content-type": "application/json",
        "x-key-id": "test-tool",
        "x-timestamp": timestamp,
        "x-request-id": request_id,
        "x-signature": signature(SECRET, method, path, timestamp, request_id, body),
    }


def post(client, env, body=None, request_id=None):
    raw = json.dumps(body or payload(env), ensure_ascii=False).encode()
    return client.post(PATH, content=raw, headers=signed(raw, request_id=request_id))


def items(batch_id):
    with Session(engine) as db:
        rows = db.exec(
            select(MaterialPushItem)
            .where(MaterialPushItem.batch_id == UUID(str(batch_id)))
            .order_by(MaterialPushItem.external_id)
        ).all()
        db.expunge_all()
        return rows


def test_batch_accepts_names_three_fields_and_signed_status(client, push_env):
    result = post(client, push_env)
    assert result.status_code == 202, result.text
    value = result.json()
    assert value["accepted_count"] == 2 and value["bc_id"] == push_env["bc_id"]
    assert value["status"] == "accepted"
    assert "signature=" not in result.text
    path = f"{PATH}/{value['batch_id']}"
    read = client.get(path, headers=signed(method="GET", path=path))
    assert read.status_code == 200 and read.json() == value
    with Session(engine) as db:
        saved = db.get(MaterialPushItem, items(value["batch_id"])[0].id)
        assert URL not in saved.url_ciphertext
        task = db.get(PendingDispatch, saved.dispatch_id)
        assert task.payload == {"item_id": str(saved.id)}


def test_public_source_host_needs_no_configuration(client, push_env):
    body = payload(push_env)
    body["materials"][0]["url"] = "https://another-r2.example/video.mp4"
    result = post(client, push_env, body)
    assert result.status_code == 202, result.text


def test_global_client_can_enter_new_tenant_without_membership_configuration(push_env):
    from app.modules.materials.push_auth import get_client, require_push_tenant
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as db:
        tenant = Tenant(name=f"new-push-{uuid4()}")
        db.add(tenant)
        db.flush()
        context = require_push_tenant(db, get_client("test-tool"), tenant.id)
        assert context.tenant_id == tenant.id
        assert context.actor_id == push_env["context"].actor_id
        member = db.get(TenantMembership, (tenant.id, context.actor_id))
        assert member.active and member.role == "operator"
        assert require_push_tenant(db, get_client("test-tool"), tenant.id) == context
        db.rollback()


@pytest.mark.parametrize(
    "mutation", ["tamper", "expired", "future", "missing", "wrong-path", "wrong-method"]
)
def test_signature_rejections_create_no_batch(client, push_env, mutation):
    raw = json.dumps(payload(push_env)).encode()
    headers = signed(raw)
    if mutation == "tamper":
        raw += b" "
    if mutation == "expired":
        headers = signed(raw, timestamp=int(time.time()) - 301)
    if mutation == "future":
        headers = signed(raw, timestamp=int(time.time()) + 601)
    if mutation == "missing":
        headers.pop("x-signature")
    if mutation == "wrong-path":
        headers = signed(raw, path=PATH + "/else")
    if mutation == "wrong-method":
        headers = signed(raw, method="GET")
    result = client.post(PATH, content=raw, headers=headers)
    assert result.status_code == 401
    with Session(engine) as db:
        assert not db.exec(
            select(MaterialPushBatch).where(
                MaterialPushBatch.tenant_id == push_env["context"].tenant_id
            )
        ).all()


def test_duplicate_request_does_not_update_latest_revision(client, push_env):
    identity = uuid4()
    first = post(client, push_env, request_id=identity)
    assert first.status_code == 202, first.text
    newer = post(client, push_env)
    assert [x.revision for x in items(newer.json()["batch_id"])] == [2, 2]
    again = post(client, push_env, request_id=identity)
    assert again.json()["batch_id"] == first.json()["batch_id"]
    changed = payload(push_env)
    changed["materials"][0]["file_name"] = "改名.mp4"
    assert post(client, push_env, changed, identity).status_code == 409
    with Session(engine) as db:
        assert (
            db.get(
                PushedMaterial, (push_env["context"].tenant_id, "video-1")
            ).latest_revision
            == 2
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-tenant",
        "empty",
        "duplicate",
        "unknown-field",
        "no-extension",
        "http-url",
        "private-ip",
        "too-many",
    ],
)
def test_invalid_batch_is_atomic_and_never_echoes_url(client, push_env, mutation):
    body = payload(push_env)
    if mutation == "unknown-tenant":
        body["tenant_name"] = "not-an-existing-tenant"
    if mutation == "empty":
        body["materials"] = []
    if mutation == "duplicate":
        body["materials"][1]["material_id"] = "video-1"
    if mutation == "unknown-field":
        body["materials"][1]["secret"] = "no"
    if mutation == "no-extension":
        body["materials"][1]["file_name"] = "no_extension"
    if mutation == "http-url":
        body["materials"][1]["url"] = "http://materials.example/video.mp4"
    if mutation == "private-ip":
        body["materials"][1]["url"] = "https://127.0.0.1/video.mp4"
    if mutation == "too-many":
        body = payload(push_env, tuple(f"id-{x}" for x in range(201)))
    result = post(client, push_env, body)
    assert result.status_code == 422, result.text
    assert "offline-sensitive-url" not in result.text
    with Session(engine) as db:
        assert not db.exec(
            select(MaterialPushItem).where(
                MaterialPushItem.tenant_id == push_env["context"].tenant_id
            )
        ).all()


def test_inactive_tenant_and_oversized_body(client, push_env):
    with Session(engine) as db, db.begin():
        db.get(Tenant, push_env["context"].tenant_id).active = False
    assert post(client, push_env).status_code == 422
    raw = b" " * (2 * 1024 * 1024 + 1)
    assert client.post(PATH, content=raw, headers=signed(raw)).status_code == 413


def test_default_bc_used_and_not_reselected_on_retry(client, push_env):
    with Session(engine) as db, db.begin():
        tenant = db.get(Tenant, push_env["context"].tenant_id)
        db.add(TenantBC(tenant_id=tenant.id, bc_id="aaa-no-authorized-connection"))
        tenant.default_bc_id = push_env["bc_id"]
    identity = uuid4()
    first = post(client, push_env, request_id=identity)
    assert first.status_code == 202, first.text
    with Session(engine) as db, db.begin():
        db.get(Tenant, push_env["context"].tenant_id).default_bc_id = None
    assert (
        post(client, push_env, request_id=identity).json()["bc_id"] == push_env["bc_id"]
    )
    # 第一个 BC 无授权直接报错，不能因其他 BC 可上传而悄悄换目标。
    assert post(client, push_env).status_code == 409


@pytest.fixture
def external_http(monkeypatch):
    import socket

    from urllib3.response import HTTPResponse

    body = b"synthetic video content"
    calls = []
    original_dns = socket.getaddrinfo

    def dns(host, *args, **kwargs):
        if host == "materials.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        return original_dns(host, *args, **kwargs)

    def request(pool, method, url, **kwargs):
        calls.append((pool.host, method, url, kwargs))
        return HTTPResponse(
            body=io.BytesIO(body),
            headers={"Content-Length": str(len(body)), "ETag": "fixed-etag"},
            status=200,
            preload_content=False,
        )

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr("urllib3.HTTPSConnectionPool.urlopen", request)
    # ffprobe 子进程边界，真正的流式字节/摘要逻辑仍执行。
    monkeypatch.setattr(
        "app.modules.materials.object_validation.subprocess.run",
        lambda *a, **k: type(
            "Probe",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "streams": [
                            {"codec_type": "video", "width": 1080, "height": 1920}
                        ],
                        "format": {
                            "duration": "4.5",
                            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        },
                    }
                ).encode(),
            },
        )(),
    )
    return body, calls


def test_external_video_runs_existing_upload_and_is_not_cleaned(
    client, push_env, redis_client, wire, external_http
):
    result = post(client, push_env, payload(push_env, ("one",)))
    assert result.status_code == 202, result.text
    identity = items(result.json()["batch_id"])[0].id
    process_item(database_engine=engine, context=push_env["context"], item_id=identity)
    row = items(result.json()["batch_id"])[0]
    assert row.status == "imported", row.error_code
    with Session(engine) as db:
        material = db.get(MaterialFile, row.material_id)
        assert material.file_name == "原名_one.mp4"
        assert material.byte_size == len(external_http[0])
        assert material.sha256 == sha256(external_http[0]).hexdigest()
        assert material.video_md5 == md5(external_http[0]).hexdigest()
        assert (material.width, material.height, material.duration) == (1080, 1920, 4.5)
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == material.id
            )
        ).one()
        assert obj.storage_provider == "external" and obj.reserved_bytes == 0
        object_id = obj.id
    wire[1].append(
        [{"video_id": "external-actual-video", "material_id": "external-mid"}]
    )
    run_source_upload(
        database_engine=engine,
        redis_client=redis_client,
        context=push_env["context"],
        material_id=row.material_id,
        object_id=object_id,
        generation=1,
        kind="upload",
    )
    assert dict(wire[0][0][2]["fields"])["file_name"] == "原名_one.mp4"
    assert dict(wire[0][0][2]["fields"])["video_url"] == URL
    with Session(engine) as db:
        assert not db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == row.material_id)
        ).all()
        assert db.get(TemporaryMaterialObject, object_id).status == "verified"
    path = f"{PATH}/{result.json()['batch_id']}"
    assert (
        client.get(path, headers=signed(method="GET", path=path)).json()["status"]
        == "completed"
    )
    process_item(database_engine=engine, context=push_env["context"], item_id=identity)
    assert len(external_http[1]) == 1


def test_late_revision_cannot_replace_current_and_history_is_preserved(
    client, push_env, external_http
):
    first = post(client, push_env, payload(push_env, ("one",)))
    body = payload(push_env, ("one",))
    body["materials"][0]["file_name"] = "新版.mp4"
    second = post(client, push_env, body)
    old, new = items(first.json()["batch_id"])[0], items(second.json()["batch_id"])[0]
    for item in (new, old):
        process_item(
            database_engine=engine, context=push_env["context"], item_id=item.id
        )
    assert len(external_http[1]) == 2
    old, new = items(first.json()["batch_id"])[0], items(second.json()["batch_id"])[0]
    assert old.material_id and new.material_id and old.material_id != new.material_id
    with Session(engine) as db:
        assert (
            db.get(
                PushedMaterial, (push_env["context"].tenant_id, "one")
            ).current_material_id
            == new.material_id
        )
        assert db.get(MaterialFile, old.material_id).file_name == "原名_one.mp4"
        from app.modules.materials.repository import deduplicate_material_statement

        current_ids = db.exec(
            deduplicate_material_statement(
                select(MaterialFile).where(
                    MaterialFile.tenant_id == push_env["context"].tenant_id,
                    MaterialFile.id.in_([old.material_id, new.material_id]),
                )
            )
        ).all()
        assert [file.id for file in current_ids] == [new.material_id]


def test_expired_worker_claim_repairs_and_revoked_key_fails_item(
    client, push_env, monkeypatch
):
    batch = post(client, push_env, payload(push_env, ("one",))).json()
    row = items(batch["batch_id"])[0]
    with Session(engine) as db, db.begin():
        item = db.get(MaterialPushItem, row.id)
        item.status, item.attempts = "validating", 1
        item.claim_token, item.claimed_until = (
            uuid4(),
            datetime.now(UTC) - timedelta(seconds=1),
        )
        assert repair_imports(db) == 1
    monkeypatch.setattr(settings, "MATERIAL_PUSH_CLIENTS", {})
    process_item(database_engine=engine, context=push_env["context"], item_id=row.id)
    assert items(batch["batch_id"])[0].error_code == "push_unauthorized"


def test_concurrent_duplicate_request_commits_one_batch(push_env):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from app.modules.materials.push_auth import authenticate
    from app.modules.materials.push_schemas import PushBatchInput
    from app.modules.materials.push_service import register_batch

    raw = json.dumps(payload(push_env)).encode()
    identity = authenticate(method="POST", path=PATH, headers=signed(raw), body=raw)
    body = PushBatchInput.model_validate_json(raw)
    started = Event()

    def retry():
        with Session(engine) as db, db.begin():
            started.set()
            return register_batch(db, identity=identity, body=body, raw_body=raw)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as db, db.begin():
            first = register_batch(db, identity=identity, body=body, raw_body=raw)
            future = pool.submit(retry)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        assert future.result(timeout=5).batch_id == first.batch_id
    assert [item.revision for item in items(first.batch_id)] == [1, 1]


def test_two_workers_only_download_once(client, push_env, external_http, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import urllib3

    row = items(post(client, push_env, payload(push_env, ("one",))).json()["batch_id"])[
        0
    ]
    started, release = Event(), Event()
    request = urllib3.HTTPSConnectionPool.urlopen

    def blocked(pool, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return request(pool, *args, **kwargs)

    monkeypatch.setattr(urllib3.HTTPSConnectionPool, "urlopen", blocked)
    kwargs = {
        "database_engine": engine,
        "context": push_env["context"],
        "item_id": row.id,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_item, **kwargs)
        try:
            assert started.wait(5)
            pool.submit(process_item, **kwargs).result(timeout=2)
        finally:
            release.set()
        first.result(timeout=5)
    assert len(external_http[1]) == 1
    assert items(row.batch_id)[0].status == "imported"


def test_failed_item_does_not_rollback_sibling_and_permissions_rechecked(
    client, push_env, external_http
):
    assert external_http[0]
    body = payload(push_env)
    body["materials"][0]["file_name"] = "容器错误.avi"
    batch = post(client, push_env, body).json()
    for row in items(batch["batch_id"]):
        process_item(
            database_engine=engine, context=push_env["context"], item_id=row.id
        )
    bad, good = items(batch["batch_id"])
    assert (bad.status, bad.error_code) == ("failed", "invalid_video")
    assert good.status == "imported"
    from app.modules.tenants.models import TenantMembership

    with Session(engine) as db, db.begin():
        db.get(
            TenantMembership,
            (push_env["context"].tenant_id, push_env["context"].actor_id),
        ).active = False
    path = f"{PATH}/{batch['batch_id']}"
    assert client.get(path, headers=signed(method="GET", path=path)).status_code == 403
    from app.core.errors import DomainError
    from app.modules.materials.push_worker import external_source_url

    with Session(engine) as db, pytest.raises(DomainError):
        external_source_url(
            db, context=push_env["context"], material_id=good.material_id
        )


def test_completed_same_content_reuses_material_version(
    client, push_env, redis_client, wire, external_http
):
    assert external_http[0]
    first = items(
        post(client, push_env, payload(push_env, ("one",))).json()["batch_id"]
    )[0]
    process_item(database_engine=engine, context=push_env["context"], item_id=first.id)
    first = items(first.batch_id)[0]
    with Session(engine) as db:
        obj = db.exec(
            select(TemporaryMaterialObject).where(
                TemporaryMaterialObject.material_id == first.material_id
            )
        ).one()
    wire[1].append([{"video_id": "reused-video", "material_id": "remote-mid"}])
    run_source_upload(
        database_engine=engine,
        redis_client=redis_client,
        context=push_env["context"],
        material_id=first.material_id,
        object_id=obj.id,
        generation=1,
        kind="upload",
    )
    second = items(
        post(client, push_env, payload(push_env, ("one",))).json()["batch_id"]
    )[0]
    process_item(database_engine=engine, context=push_env["context"], item_id=second.id)
    assert items(second.batch_id)[0].material_id == first.material_id
    assert len(wire[0]) == 1


@pytest.mark.parametrize(
    "target",
    [
        "http://materials.example.test/a.mp4",
        "https://materials.example.test:444/a.mp4",
        "https://materials.example.test/a.mp4#fragment",
        "https://u:p@materials.example.test/a.mp4",
        "https://169.254.169.254/a.mp4",
        "https://[::1]/a.mp4",
        "https://127.0.0.1/a.mp4",
        "https://materials.example.test/a\n.mp4",
    ],
)
def test_url_security_boundary(target):
    from app.core.errors import DomainError
    from app.modules.materials.push_transport import validate_url

    with pytest.raises(DomainError) as caught:
        validate_url(target)
    assert caught.value.code == "push_url_invalid"


def test_dns_all_results_must_be_public(monkeypatch):
    import socket

    from app.core.errors import DomainError
    from app.modules.materials.push_transport import public_address

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
        ],
    )
    with pytest.raises(DomainError) as caught:
        public_address("materials.example.test")
    assert caught.value.code == "push_url_unreachable"


def test_unknown_upload_is_not_reissued_and_new_url_renews_source(
    client, push_env, external_http
):
    from app.modules.materials.ingest_models import IngestSessionFile
    from app.modules.materials.push_worker import external_source_url

    assert external_http[0]
    first = items(
        post(client, push_env, payload(push_env, ("one",))).json()["batch_id"]
    )[0]
    process_item(database_engine=engine, context=push_env["context"], item_id=first.id)
    first = items(first.batch_id)[0]
    with Session(engine) as db, db.begin():
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == first.material_id
            )
        ).one()
        row.status = "result_unknown"
        dispatch_id = row.dispatch_id
    body = payload(push_env, ("one",))
    new_url = "https://materials.example.test/video.mp4?signature=renewed"
    body["materials"][0]["url"] = new_url
    second = items(post(client, push_env, body).json()["batch_id"])[0]
    process_item(database_engine=engine, context=push_env["context"], item_id=second.id)
    assert items(second.batch_id)[0].material_id == first.material_id
    with Session(engine) as db:
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == first.material_id
            )
        ).one()
        assert row.status == "result_unknown" and row.dispatch_id == dispatch_id
        assert (
            external_source_url(
                db, context=push_env["context"], material_id=first.material_id
            )
            == new_url
        )


@pytest.mark.parametrize(
    "case,code",
    [
        ("redirect", "push_url_unreachable"),
        ("encoding", "push_url_unreachable"),
        ("too-large", "push_file_too_large"),
        ("truncated", "push_file_incomplete"),
        ("empty", "push_file_incomplete"),
    ],
)
def test_stream_rejects_unsafe_or_incomplete_response(
    external_http, monkeypatch, case, code
):
    assert external_http[0]
    from urllib3.response import HTTPResponse

    from app.core.errors import DomainError
    from app.modules.materials.push_transport import inspect_external

    headers = {}
    if case == "encoding":
        headers["Content-Encoding"] = "gzip"
    if case == "too-large":
        headers["Content-Length"] = str(settings.MATERIAL_URL_MAX_UPLOAD_BYTES + 1)
    if case == "truncated":
        headers["Content-Length"] = "100"
    monkeypatch.setattr(
        "urllib3.HTTPSConnectionPool.urlopen",
        lambda *a, **k: HTTPResponse(
            body=io.BytesIO(b"" if case == "empty" else b"abc"),
            headers=headers,
            status=302 if case == "redirect" else 200,
            preload_content=False,
            enforce_content_length=False,
        ),
    )
    with pytest.raises(DomainError) as caught:
        inspect_external(URL, "original.mp4")
    assert caught.value.code == code


def test_transport_pins_ip_preserves_host_and_disables_redirects(external_http):
    from app.modules.materials.push_transport import inspect_external

    facts = inspect_external(URL, "original.mp4")
    host, method, target, kwargs = external_http[1][0]
    assert host == "93.184.216.34" and method == "GET"
    assert target.startswith("/video.mp4?")
    assert kwargs["headers"]["Host"] == "materials.example.test"
    assert kwargs["redirect"] is False and kwargs["retries"] is False
    assert facts.mime_type == "video/mp4"


def test_repair_limit_does_not_starve_expired_claim(client, push_env):
    rows = items(post(client, push_env).json()["batch_id"])
    with Session(engine) as db, db.begin():
        # 正常排队项 UUID 较小，但不能先占掉修复 LIMIT。
        stale = db.get(MaterialPushItem, max(row.id for row in rows))
        stale.status, stale.attempts = "validating", 1
        stale.claim_token, stale.claimed_until = (
            uuid4(),
            datetime.now(UTC) - timedelta(seconds=1),
        )
        assert repair_imports(db, limit=1) == 1
        assert stale.status == "queued" and stale.claim_token is None


def test_external_original_preview_and_transport_mutations_are_guarded(
    client, push_env, external_http
):
    from app.core.errors import DomainError
    from app.modules.materials.catalog import original_preview
    from app.modules.materials.ingest_models import IngestSessionFile
    from app.modules.materials.ingest_schemas import IngestIdentity
    from app.modules.materials.ingest_transport import _locked

    assert external_http[0]
    row = items(post(client, push_env, payload(push_env, ("one",))).json()["batch_id"])[
        0
    ]
    process_item(database_engine=engine, context=push_env["context"], item_id=row.id)
    row = items(row.batch_id)[0]
    with Session(engine) as db:
        assert (
            original_preview(
                db, context=push_env["context"], material_id=row.material_id
            ).url
            == URL
        )
        parent = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == row.material_id
            )
        ).one()
        with pytest.raises(DomainError) as caught:
            _locked(
                db,
                context=push_env["context"],
                material_id=row.material_id,
                session_id=parent.session_id,
                identity=IngestIdentity(generation=1, operation_revision=0),
            )
        assert caught.value.code == "push_external_read_only"
