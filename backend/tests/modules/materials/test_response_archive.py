"""Full received response retention at real SDK/MCP wire boundaries."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.modules.materials.models import MaterialResponseArchive
from app.modules.materials.response_archive import read_material_response
from app.modules.tenants.models import TenantMembership
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_material_upload_worker import source_env as source_env
from tests.modules.materials.test_url_ingest import info, operation, run
from tests.modules.materials.test_url_ingest import url_env as url_env


def archives(db, env):
    return db.exec(
        select(MaterialResponseArchive)
        .where(MaterialResponseArchive.material_id == env["material_id"])
        .order_by(MaterialResponseArchive.received_at, MaterialResponseArchive.id)
    ).all()


def set_role(db, context, role):
    db.get(TenantMembership, (context.tenant_id, context.actor_id)).role = role
    db.commit()


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("outcome", ["success", "business_error", "missing_vid"])
def test_full_response_keeps_unknown_fields_and_exact_text_without_extra_call(
    gateway_case,
    gateway_wire,
    url_env,
    redis_client,
    database_engine,
    monkeypatch,
    outcome,
    caplog,
):
    row = {
        "video_id": "actual-source-vid",
        "size": 12345,
        "signature": "a" * 32,
        "extra": {"unknown": [True, None, "完整中文字段"]},
        "preview_url": "https://example.test/video?signature=private-response-value",
    }
    if outcome == "missing_vid":
        del row["video_id"]
    envelope = {
        "code": 0 if outcome != "business_error" else 40002,
        "message": "保留上游原文",
        "request_id": "saved-request",
        "data": [row],
    }
    body = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    calls = []
    if gateway_case[1].channel == "OFFICIAL_API":

        def send(_pool, _method, _url, **_kwargs):
            calls.append(True)
            return HTTPResponse(body=body.encode(), status=200)

        monkeypatch.setattr("urllib3.PoolManager.request", send)
    else:
        gateway_wire["wire"].results["file_video_ad_upload"].append(
            {"content": [{"type": "text", "text": body}], "isError": False}
        )
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == ("succeeded" if outcome == "success" else "result_unknown")
    with Session(database_engine) as db:
        saved = archives(db, url_env)
        assert len(saved) == 1
        record = saved[0]
        assert (record.operation_id, record.advertiser_id, record.connection_id) == (
            op.id,
            gateway_case[2],
            gateway_case[1].connection_id,
        )
        assert "private-response-value" not in record.body_ciphertext
        assert "private-response-value" not in repr(record)
        set_role(db, url_env["context"], "operator")
        with pytest.raises(DomainError, match="当前角色"):
            read_material_response(
                db,
                context=url_env["context"],
                bc_id=url_env["bc_id"],
                material_id=url_env["material_id"],
                response_id=record.id,
            )
        set_role(db, url_env["context"], "tenant_admin")
        raw = read_material_response(
            db,
            context=url_env["context"],
            bc_id=url_env["bc_id"],
            material_id=url_env["material_id"],
            response_id=record.id,
        )
        if gateway_case[1].channel == "OFFICIAL_API":
            assert raw == body.encode()
            assert record.http_status == 200
        else:
            result = json.loads(raw)
            assert result["content"][0]["text"] == body
        for wrong_scope in ({"bc_id": "other-bc"}, {"material_id": uuid4()}):
            kwargs = {
                "context": url_env["context"],
                "bc_id": url_env["bc_id"],
                "material_id": url_env["material_id"],
                "response_id": record.id,
            }
            kwargs.update(wrong_scope)
            with pytest.raises(DomainError, match="未找到"):
                read_material_response(db, **kwargs)
        with pytest.raises(DomainError):
            read_material_response(
                db,
                context=replace(url_env["context"], tenant_id=uuid4()),
                bc_id=url_env["bc_id"],
                material_id=url_env["material_id"],
                response_id=record.id,
            )
    assert "private-response-value" not in caplog.text + repr(op.remote_response)
    if gateway_case[1].channel == "OFFICIAL_API":
        assert len(calls) == 1
    else:
        assert (
            len([c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"])
            == 1
        )


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_failed_upload_and_recovery_queries_are_separate_complete_archives(
    gateway_case,
    gateway_wire,
    url_env,
    redis_client,
    database_engine,
):
    assert gateway_case[1].channel == "OFFICIAL_MCP"
    gateway_wire["wire"].results["file_video_ad_upload"].append(
        {
            "content": [{"type": "text", "text": "upstream diagnostic raw message"}],
            "isError": True,
        }
    )
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "result_unknown"
    row = {**info()["list"][0], "file_name": op.remote_response["remote_name"]}
    search = {
        "code": 0,
        "data": {
            "list": [row],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        },
    }
    gateway_wire["wire"].results["file_video_ad_search"].append(
        {"content": [], "structuredContent": search}
    )
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    gateway_wire["wire"].results["file_video_ad_info_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": info()}}
    )
    run(url_env, redis_client, kind="verify", operation_id=op.id)
    assert operation(url_env).status == "succeeded"
    with Session(database_engine) as db:
        saved = archives(db, url_env)
        assert [r.operation for r in saved] == [
            "materials.upload_video_url",
            "materials.search_videos",
            "materials.get_videos",
        ]
        set_role(db, url_env["context"], "tenant_admin")
        first = json.loads(
            read_material_response(
                db,
                context=url_env["context"],
                bc_id=url_env["bc_id"],
                material_id=url_env["material_id"],
                response_id=saved[0].id,
            )
        )
        assert first["content"][0]["text"] == "upstream diagnostic raw message"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
def test_archive_database_retry_does_not_repeat_remote_upload(
    gateway_case,
    gateway_wire,
    url_env,
    redis_client,
    database_engine,
):
    assert gateway_case[1].channel == "OFFICIAL_MCP"
    failures = []

    def fail_once(_conn, _cursor, statement, _params, _context, _many):
        if "INSERT INTO material_response_archive" in statement and not failures:
            failures.append(True)
            raise RuntimeError("synthetic archive transaction failure")

    gateway_wire["wire"].results["file_video_ad_upload"].append(
        {
            "content": [],
            "structuredContent": {"code": 0, "data": {"video_id": "actual-source-vid"}},
        }
    )
    event.listen(Engine, "before_cursor_execute", fail_once)
    try:
        run(url_env, redis_client)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_once)
    assert failures and operation(url_env).status == "succeeded"
    with Session(database_engine) as db:
        assert len(archives(db, url_env)) == 1
    assert (
        len([c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]) == 1
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_unavailable_archive_keeps_sent_upload_unknown_without_resend(
    gateway_case, gateway_wire, url_env, redis_client, database_engine, caplog
):
    failures = []

    def fail_insert(_conn, _cursor, statement, _params, _context, _many):
        if "INSERT INTO material_response_archive" in statement:
            failures.append(True)
            raise RuntimeError("synthetic-private-database-error")

    if gateway_case[1].channel == "OFFICIAL_MCP":
        gateway_wire["wire"].results["file_video_ad_upload"].append(
            {
                "content": [],
                "structuredContent": {
                    "code": 0,
                    "data": {"video_id": "actual-source-vid"},
                },
            }
        )
    event.listen(Engine, "before_cursor_execute", fail_insert)
    try:
        run(url_env, redis_client)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_insert)
    assert len(failures) == 2
    assert operation(url_env).status == "result_unknown"
    with Session(database_engine) as db:
        assert not archives(db, url_env)
    calls = (
        gateway_wire["sdk_calls"]
        if gateway_case[1].channel == "OFFICIAL_API"
        else [c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"]
    )
    assert len(calls) == 1
    assert "synthetic-private-database-error" not in caplog.text


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("status", [200, 503])
def test_sdk_retains_unparseable_and_http_error_bodies(
    gateway_case, url_env, redis_client, database_engine, monkeypatch, status
):
    assert gateway_case[1].channel == "OFFICIAL_API"
    body = b"<html>upstream malformed response</html>\n"
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *_args, **_kwargs: HTTPResponse(body=body, status=status),
    )
    run(url_env, redis_client)
    assert operation(url_env).status == "result_unknown"
    with Session(database_engine) as db:
        (record,) = archives(db, url_env)
        assert record.http_status == status
        assert (
            read_material_response(
                db,
                context=url_env["context"],
                bc_id=url_env["bc_id"],
                material_id=url_env["material_id"],
                response_id=record.id,
            )
            == body
        )
