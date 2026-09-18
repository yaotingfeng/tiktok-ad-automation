"""目标 URL 转存及核验在真实 PG/Redis、SDK/MCP 传输边界保留完整响应。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.modules.accounts.connection_models import BCConnectionBinding, BCDefaultRoute
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import MaterialFile, MaterialResponseArchive
from app.modules.materials.response_archive import read_material_response
from tests.integrations.tiktok.gateway_support import business_calls
from tests.integrations.tiktok.gateway_support import database_engine as database_engine
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_native_share_session import share_case as share_case
from tests.modules.materials.test_native_share_session import share_wire as share_wire
from tests.modules.materials.test_readiness import target


@pytest.fixture
def relay_case(share_case, gateway_case, database_engine, monkeypatch):
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.vetted.example"})
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                **settings.TIKTOK_CALL_POLICIES["endpoints"],
                "materials.upload_video_url": {"lease_ms": 970000},
            },
        },
    )
    with Session(database_engine) as db, db.begin():
        source = db.get(MaterialFile, share_case["material_id"])
        source.sha256 = "b" * 64
        source.digest_verified_at = datetime.now(UTC)
        bc_id = "2234567890123456789"
        db.add(TenantBC(tenant_id=source.tenant_id, bc_id=bc_id))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=source.tenant_id,
                bc_id=bc_id,
                connection_id=share_case["connection_id"],
                kind=gateway_case[1].channel,
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=source.tenant_id,
                bc_id=bc_id,
                connection_id=share_case["connection_id"],
            )
        )
        material = MaterialFile(
            **(
                source.model_dump()
                | {"id": uuid4(), "bc_id": bc_id, "object_key": str(uuid4())}
            )
        )
        db.add(material)
        db.flush()
        env = {**share_case, "bc_id": bc_id, "material_id": material.id}
        env["target"] = target(db, env, advertiser_id="90071992547409933")
    return env


def video(vid, **extra):
    return {
        "video_id": vid,
        "signature": "a" * 32,
        "displayable": True,
        "width": 720,
        "height": 1280,
        "duration": 4.5,
        "size": 120,
        "format": "mp4",
        "preview_url": "https://media.vetted.example/video?signature=private-response",
        **extra,
    }


def wire_responses(monkeypatch, gateway_case, gateway_wire, responses):
    """只替换 HTTP；业务反序列化、归档、持久状态与 Redis 准入全部真实执行。"""
    bodies = [
        json.dumps(body, ensure_ascii=False, indent=2) + "\n" for _, body in responses
    ]
    if gateway_case[1].channel == "OFFICIAL_MCP":
        names = {c.operation: c.tool_name for c in load_tool_contracts()}
        for (operation, _), body in zip(responses, bodies, strict=True):
            gateway_wire["wire"].results[names[operation]].append(
                {"content": [{"type": "text", "text": body}], "isError": False}
            )
    else:
        pending = iter(bodies)

        def send(_pool, method, url, **kwargs):
            gateway_wire["sdk_calls"].append((method, url, kwargs))
            return HTTPResponse(body=next(pending).encode(), status=200)

        monkeypatch.setattr("urllib3.PoolManager.request", send)
    return bodies


def saved_responses(db, env):
    return db.exec(
        select(MaterialResponseArchive)
        .where(MaterialResponseArchive.tenant_id == env["context"].tenant_id)
        .order_by(MaterialResponseArchive.received_at, MaterialResponseArchive.id)
    ).all()


def assert_archive(db, env, record, operation, body, channel):
    assert (
        record.operation_id,
        record.bc_id,
        record.material_id,
        record.advertiser_id,
    ) == (operation.id, "2234567890123456789", env["material_id"], "90071992547409933")
    assert record.connection_id == env["connection_id"]
    assert "private-response" not in record.body_ciphertext
    raw = read_material_response(
        db,
        context=env["context"],
        bc_id=env["bc_id"],
        material_id=env["material_id"],
        response_id=record.id,
    )
    if channel == "OFFICIAL_API":
        assert raw == body.encode()
    else:
        assert json.loads(raw)["content"][0]["text"] == body


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("outcome", ["success", "business_error", "missing_vid"])
def test_target_upload_retains_exact_response_under_target_scope(
    relay_case,
    gateway_case,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    outcome,
    caplog,
):
    # 缺失 observer 会丢掉整份目标响应；错误绑定源账户则导致原本成功的上传无法完成。
    row = {
        "video_id": "actual-target-vid",
        "unknown": {"说明": [True, None]},
        "preview_url": "private-response",
    }
    if outcome == "missing_vid":
        del row["video_id"]
    bodies = wire_responses(
        monkeypatch,
        gateway_case,
        gateway_wire,
        [
            (
                "materials.get_videos",
                {"code": 0, "data": {"list": [video(f"vid-{relay_case['source']}")]}},
            ),
            (
                "materials.upload_video_url",
                {
                    "code": 40002 if outcome == "business_error" else 0,
                    "message": "上游原文",
                    "request_id": "target-upload-request",
                    "data": [row],
                },
            ),
        ],
    )
    prepared = queue(relay_case, relay_case["target"])
    assert prepared.task_id is not None, prepared
    run(relay_case, redis_client, prepared.task_id, kind="prepare")
    dist, operation, mapping = state(prepared.task_id)
    assert operation.status == (
        "succeeded" if outcome == "success" else "result_unknown"
    ), operation.remote_response
    if outcome != "success":
        assert dist.status == "result_unknown" and mapping is None
        assert not operation.remote_response.get("definite_no_effect")
        assert operation.remote_response.get("send_armed") is True
        assert dist.reason_code == (
            "mcp_business_error"
            if outcome == "business_error" and gateway_case[1].channel == "OFFICIAL_MCP"
            else "material_response_unknown"
        )
    with Session(database_engine) as db:
        saved = saved_responses(db, relay_case)
        assert len(saved) == 1
        assert saved[0].operation == "materials.upload_video_url"
        assert_archive(
            db, relay_case, saved[0], operation, bodies[1], gateway_case[1].channel
        )
    assert "private-response" not in caplog.text + repr(operation.remote_response)
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_target_reconciliation_keeps_search_and_info_as_separate_archives(
    relay_case, gateway_case, gateway_wire, database_engine, redis_client, monkeypatch
):
    wire_responses(
        monkeypatch,
        gateway_case,
        gateway_wire,
        [
            (
                "materials.get_videos",
                {"code": 0, "data": {"list": [video(f"vid-{relay_case['source']}")]}},
            ),
            (
                "materials.upload_video_url",
                {"code": 40002, "message": "received upstream error", "data": {}},
            ),
        ],
    )
    prepared = queue(relay_case, relay_case["target"])
    assert prepared.task_id is not None, prepared
    run(relay_case, redis_client, prepared.task_id, kind="prepare")
    operation = state(prepared.task_id)[1]
    row = video("actual-target-vid", file_name=operation.remote_response["remote_name"])
    bodies = wire_responses(
        monkeypatch,
        gateway_case,
        gateway_wire,
        [
            (
                "materials.search_videos",
                {
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
                },
            ),
            ("materials.get_videos", {"code": 0, "data": {"list": [row]}}),
        ],
    )
    run(relay_case, redis_client, prepared.task_id)
    run(relay_case, redis_client, prepared.task_id)
    dist, operation, mapping = state(prepared.task_id)
    assert dist.status == "ready" and mapping.video_id == "actual-target-vid"
    with Session(database_engine) as db:
        saved = saved_responses(db, relay_case)
        assert [r.operation for r in saved] == [
            "materials.upload_video_url",
            "materials.search_videos",
            "materials.get_videos",
        ]
        for record, body in zip(saved[1:], bodies, strict=True):
            assert_archive(
                db, relay_case, record, operation, body, gateway_case[1].channel
            )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_native_source_read_does_not_use_target_archive_scope(
    share_case, share_wire, database_engine, redis_client
):
    # 原生共享会在同一 gateway 读源账户；误绑目标归档会阻断真实共享。
    assert share_wire
    prepared = queue(share_case, share_case["target"])
    run(share_case, redis_client, prepared.task_id, kind="prepare")
    dist, operation, _ = state(prepared.task_id)
    assert dist.status == "verifying", operation.remote_response
    assert operation.remote_response["share_acknowledged"] is True
    with Session(database_engine) as db:
        assert not saved_responses(db, share_case)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_target_archive_failure_preserves_armed_unknown_without_second_upload(
    relay_case,
    gateway_case,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    caplog,
):
    # 归档数据库故障不能伪造未发送事实，也不能因本地重试重复上传。
    wire_responses(
        monkeypatch,
        gateway_case,
        gateway_wire,
        [
            (
                "materials.get_videos",
                {"code": 0, "data": {"list": [video(f"vid-{relay_case['source']}")]}},
            ),
            (
                "materials.upload_video_url",
                {"code": 0, "data": [{"video_id": "actual-target-vid"}]},
            ),
        ],
    )
    prepared = queue(relay_case, relay_case["target"])
    failures = []

    def unavailable(_conn, _cursor, statement, _params, _context, _many):
        if "INSERT INTO material_response_archive" in statement:
            failures.append(True)
            raise RuntimeError("synthetic-private-archive-failure")

    event.listen(Engine, "before_cursor_execute", unavailable)
    try:
        run(relay_case, redis_client, prepared.task_id, kind="prepare")
    finally:
        event.remove(Engine, "before_cursor_execute", unavailable)
    dist, operation, mapping = state(prepared.task_id)
    assert len(failures) == 2
    assert operation.status == dist.status == "result_unknown"
    assert operation.remote_response["send_armed"] is True
    assert not operation.remote_response.get("definite_no_effect")
    assert dist.reason_code == "material_response_archive_failed"
    assert mapping is None
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 2
    # 队列重投同一 prepare 也只能保留待核查状态，不能再次读源或上传。
    run(relay_case, redis_client, prepared.task_id, kind="prepare")
    dist, operation, mapping = state(prepared.task_id)
    assert operation.status == dist.status == "result_unknown"
    assert operation.remote_response["send_armed"] is True
    assert not operation.remote_response.get("definite_no_effect")
    assert mapping is None
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 2
    assert "synthetic-private-archive-failure" not in caplog.text
    with Session(database_engine) as db:
        assert not saved_responses(db, relay_case)
