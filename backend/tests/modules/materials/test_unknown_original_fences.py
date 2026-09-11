"""双通道真实 HTTP/PG/Redis：UNKNOWN 用途不随本地时钟或清理入口消失。"""

# ruff: noqa: F401,F811 -- composed actual database/channel fixtures

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, delete, select
from urllib3.exceptions import ReadTimeoutError

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.materials.cleanup import _active_uses, run_cleanup, schedule_cleanup
from app.modules.materials.cleanup_abandoned import abandon_transport
from app.modules.materials.cleanup_reconcile import scan_abandoned_objects
from app.modules.materials.ingest_models import (
    IngestSession,
    ObjectBudget,
    ObjectCleanup,
    OriginalUse,
    TemporaryMaterialObject,
    canonical_object_key,
)
from app.modules.materials.ingest_schemas import IngestIdentity
from app.modules.materials.ingest_transport import cancel_file
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from app.modules.materials.object_budget import mark_object_stored, reserve_object
from app.modules.materials.object_uses import acquire_original_use
from app.modules.tenants.models import AuditEvent
from tests.integrations.tiktok.gateway_support import business_calls
from tests.modules.materials.test_material_upload_worker import (
    app_config,
    database_engine,
    enqueue_upload,
    gateway_case,
    gateway_wire,
    policy,
    synthetic_contract,
    url_env,
)
from tests.modules.materials.test_material_upload_worker import (
    source_env as channel_source_env,
)
from tests.modules.materials.test_url_ingest import CONTENT, info, operation, run


@pytest.fixture
def source_env(channel_source_env, database_engine):
    # 原件身份插入后不可变，必须在创建原件前确定规范存储键。
    env = channel_source_env
    with Session(database_engine) as db, db.begin():
        material = db.get(MaterialFile, env["material_id"])
        material.object_key = canonical_object_key(
            material.tenant_id, material.bc_id, material.id, 1
        )
    return env


@pytest.fixture
def charged_original(url_env, database_engine, monkeypatch):
    """通过生产预算函数建账，不能只改 object.reserved_bytes 冒充额度证明。"""
    monkeypatch.setattr(settings, "MATERIAL_CLEANUP_ENABLED", True)
    with Session(database_engine) as db, db.begin():
        obj = db.get(TemporaryMaterialObject, url_env["object_id"])
        obj.reserved_bytes = 0
        db.flush()
        assert reserve_object(
            db, context=url_env["context"], object_id=obj.id, byte_size=len(CONTENT)
        )
        mark_object_stored(
            db, context=url_env["context"], object_id=obj.id, actual_bytes=len(CONTENT)
        )
        obj.status = "verified"
    try:
        yield url_env
    finally:
        with Session(database_engine) as db, db.begin():
            db.exec(
                delete(AuditEvent).where(
                    AuditEvent.tenant_id == url_env["context"].tenant_id
                )
            )
            tenant = db.get(ObjectBudget, f"tenant:{url_env['context'].tenant_id}")
            global_budget = db.get(ObjectBudget, "global")
            if tenant:
                global_budget.reserved_bytes -= tenant.reserved_bytes
                global_budget.stored_bytes -= tenant.stored_bytes
                db.delete(tenant)


def assert_reserved(db, env, operation_id):
    use = db.exec(
        select(OriginalUse).where(OriginalUse.operation_id == operation_id)
    ).one()
    assert use.status == "active" and use.released_at is None
    obj = db.get(TemporaryMaterialObject, env["object_id"])
    assert obj.reserved_bytes == len(CONTENT) and obj.reservation_released_at is None
    assert use.id in {row.id for row in _active_uses(db, obj)}
    tenant = db.get(ObjectBudget, f"tenant:{env['context'].tenant_id}")
    batch = db.get(IngestSession, env["session_id"])
    assert tenant.reserved_bytes == tenant.stored_bytes == len(CONTENT)
    assert batch.reserved_bytes == batch.stored_bytes == len(CONTENT)
    global_budget = db.get(ObjectBudget, "global")
    assert global_budget.reserved_bytes >= tenant.reserved_bytes
    assert global_budget.stored_bytes >= tenant.stored_bytes


def expire_local_clocks(database_engine, env, operation_id):
    with Session(database_engine) as db, db.begin():
        old = datetime.now(UTC) - timedelta(days=3)
        use = db.exec(
            select(OriginalUse).where(OriginalUse.operation_id == operation_id)
        ).one()
        use.expires_at = use.permission_issued_at = old
        obj = db.get(TemporaryMaterialObject, env["object_id"])
        obj.reserved_at = obj.received_at = obj.digest_verified_at = old
        if obj.claim_token:
            obj.claimed_until = old
        op = db.get(MaterialAssetOperation, operation_id)
        if op.attempt_token:
            op.claimed_until = old


class ForbidDelete:
    def __getattr__(self, name):
        if name in {"delete_object", "abort_multipart_upload", "head_object"}:
            pytest.fail(f"UNKNOWN 原件不应进入远端清理：{name}")
        raise AttributeError(name)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_expired_unknown_upload_survives_all_cleanup_entrypoints(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
    monkeypatch,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    channel = gateway_case[1].channel
    if channel == "OFFICIAL_MCP":
        gateway_wire["wire"].disconnect_after_accept("file_video_ad_upload")
    else:

        def accepted_then_lost(_pool, method, url, **kwargs):
            gateway_wire["sdk_calls"].append((method, url, kwargs))
            raise ReadTimeoutError(None, "/upload", "synthetic accepted response lost")

        monkeypatch.setattr("urllib3.PoolManager.request", accepted_then_lost)
    run(env, redis_client)
    op = operation(env)
    assert op.status == "result_unknown" and op.remote_response["send_armed"] is True
    expire_local_clocks(database_engine, env, op.id)
    with Session(database_engine) as db, db.begin():
        assert_reserved(db, env, op.id)
        result = scan_abandoned_objects(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            enqueue=True,
            now=datetime.now(UTC) + timedelta(days=4),
        )
        target = next(
            row for row in result["items"] if row["object_id"] == str(env["object_id"])
        )
        assert not target["queued"] and target["reason"] == "source_result_unknown"
        with pytest.raises(DomainError):
            schedule_cleanup(db, object_id=env["object_id"], source_receipt_id=op.id)
        obj = db.get(TemporaryMaterialObject, env["object_id"])
        identity = IngestIdentity(
            generation=obj.generation,
            upload_id=obj.s3_upload_id,
            operation_revision=obj.revision,
        )
    cancel_file(
        database_engine=database_engine,
        context=env["context"],
        session_id=env["session_id"],
        material_id=env["material_id"],
        identity=identity,
    )
    with Session(database_engine) as db, db.begin():
        cleanup = db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).one()
        cleanup_id = cleanup.id
        with pytest.raises(DomainError):
            abandon_transport(
                db, db.get(TemporaryMaterialObject, env["object_id"]), cleanup
            )
    run_cleanup(
        database_engine=database_engine, cleanup_id=cleanup_id, s3=ForbidDelete()
    )
    run(env, redis_client)
    assert len(business_calls(gateway_wire, channel)) == 1
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)
        assert db.get(ObjectCleanup, cleanup_id).delete_sent_at is None


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("bad", ["empty", "digest", "size", "advertiser"])
def test_known_vid_with_incomplete_readback_never_releases_original(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
    bad,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    enqueue_upload(gateway_case, gateway_wire)
    run(env, redis_client)
    op = operation(env)
    data = (
        {"list": []}
        if bad == "empty"
        else info(
            **{
                "digest": {"signature": "f" * 32},
                "size": {"size": len(CONTENT) + 1},
                "advertiser": {"advertiser_id": "another-advertiser"},
            }[bad]
        )
    )
    gateway_wire["sdk_data"]["data"] = data
    gateway_wire["wire"].results["file_video_ad_info_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )
    run(env, redis_client, kind="verify", operation_id=op.id)
    assert operation(env).status == "verifying"
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)
        assert not db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).all()


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_exact_completion_releases_only_its_operation_and_cleanup_waits_for_other_use(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    enqueue_upload(gateway_case, gateway_wire)
    run(env, redis_client)
    op = operation(env)
    with Session(database_engine) as db, db.begin():
        other = acquire_original_use(
            db,
            context=env["context"],
            object_id=env["object_id"],
            purpose="ingest",
            operation_id=uuid4(),
        )
        other.expires_at = datetime.now(UTC) - timedelta(days=3)
        other_id = other.id
    gateway_wire["sdk_data"]["data"] = info()
    gateway_wire["wire"].results["file_video_ad_info_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": info()}}
    )
    run(env, redis_client, kind="verify", operation_id=op.id)
    assert operation(env).status == "succeeded"
    with Session(database_engine) as db:
        assert (
            db.exec(select(OriginalUse).where(OriginalUse.operation_id == op.id))
            .one()
            .status
            == "released"
        )
        assert db.get(OriginalUse, other_id).status == "active"
        cleanup_id = db.exec(
            select(ObjectCleanup.id).where(
                ObjectCleanup.material_id == env["material_id"]
            )
        ).one()
    run_cleanup(
        database_engine=database_engine, cleanup_id=cleanup_id, s3=ForbidDelete()
    )
    with Session(database_engine) as db:
        assert db.get(ObjectCleanup, cleanup_id).error_code == "original_in_use"
        assert db.get(TemporaryMaterialObject, env["object_id"]).reserved_bytes == len(
            CONTENT
        )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_late_complete_receipt_cannot_release_use_or_replace_successor_claim(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
    monkeypatch,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    channel = gateway_case[1].channel
    enqueue_upload(gateway_case, gateway_wire)
    if channel == "OFFICIAL_MCP":
        gateway_wire["wire"].delay = 0.8
    else:
        import urllib3

        original = urllib3.PoolManager.request

        def delayed(*args, **kwargs):
            response = original(*args, **kwargs)
            time.sleep(0.8)
            return response

        monkeypatch.setattr(urllib3.PoolManager, "request", delayed)
    successor = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, env, redis_client)
        until = time.monotonic() + 8
        while not business_calls(gateway_wire, channel):
            assert time.monotonic() < until
            time.sleep(0.005)
        op = operation(env)
        with Session(database_engine) as db, db.begin():
            current = db.get(MaterialAssetOperation, op.id)
            current.status = "result_unknown"
            current.attempt_token = successor
            current.claimed_until = datetime.now(UTC) + timedelta(seconds=60)
        second = pool.submit(run, env, redis_client)
        second.result(5)
        first.result(8)
    op = operation(env)
    assert op.attempt_token == successor and op.status == "result_unknown"
    assert op.remote_response["video_id"] == "actual-source-vid"
    assert len(business_calls(gateway_wire, channel)) == 1
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_actual_receipt_survives_http_cleanup_failure_without_releasing_use(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
    monkeypatch,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    enqueue_upload(gateway_case, gateway_wire)
    if gateway_case[1].channel == "OFFICIAL_MCP":
        gateway_wire["wire"].close_failure = True
    else:
        import urllib3

        original = urllib3.PoolManager.clear

        def failed_clear(pool):
            original(pool)
            assert operation(env).remote_response["video_id"] == "actual-source-vid"
            raise RuntimeError("synthetic cleanup failure")

        monkeypatch.setattr(urllib3.PoolManager, "clear", failed_clear)
    run(env, redis_client)
    op = operation(env)
    assert (
        op.status == "verifying"
        and op.remote_response["video_id"] == "actual-source-vid"
    )
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_real_database_completion_failure_rolls_back_use_and_cleanup_together(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    enqueue_upload(gateway_case, gateway_wire)
    run(env, redis_client)
    op = operation(env)
    # 制造实际PG计数冲突，而非mock Session/commit；完成事务仍必须全部回滚。
    with Session(database_engine) as db, db.begin():
        batch = db.get(IngestSession, env["session_id"])
        batch.ready_count = batch.accepted_count
    gateway_wire["sdk_data"]["data"] = info()
    gateway_wire["wire"].results["file_video_ad_info_get"].append(
        {"content": [], "structuredContent": {"code": 0, "data": info()}}
    )
    run(env, redis_client, kind="verify", operation_id=op.id)
    assert operation(env).status == "verifying"
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)
        assert not db.exec(
            select(ObjectCleanup).where(ObjectCleanup.material_id == env["material_id"])
        ).all()


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "result_kind",
    [
        "empty",
        "multiple",
        "changed_total",
        "duplicate",
        "legacy",
        "bounded",
        "stable",
        "unknown_count",
    ],
)
def test_unknown_search_requires_complete_stable_single_identity(
    gateway_case,
    gateway_wire,
    charged_original,
    synthetic_contract,
    redis_client,
    database_engine,
    result_kind,
):
    assert synthetic_contract.category == "SYNTHETIC"
    env = charged_original
    # 上游接受但没有可解释的唯一实际ID，保持已发送 UNKNOWN。
    gateway_wire["sdk_data"]["data"] = {}
    gateway_wire["wire"].results["file_video_ad_upload"].append(
        {"content": [], "structuredContent": {"code": 0, "data": {}}}
    )
    run(env, redis_client)
    op = operation(env)
    assert op.status == "result_unknown" and not op.remote_response.get("video_id")
    candidate = info(file_name=op.remote_response["remote_name"])["list"][0]
    if result_kind in {"changed_total", "duplicate", "stable", "legacy", "bounded"}:
        first = [candidate] + [
            {**candidate, "video_id": f"other-{i}", "file_name": "other.mp4"}
            for i in range(99)
        ]
        # 两个物理页总页数相同、总行数变化，不能把旧唯一候选当完整快照。
        pages = [
            (first, 1, 101, 2),
            (
                [
                    {**candidate, "video_id": f"last-{i}", "file_name": "other.mp4"}
                    for i in range(2)
                ],
                2,
                102,
                2,
            ),
        ]
        if result_kind == "duplicate":
            pages[1] = ([{**candidate, "file_name": "other.mp4"}], 2, 101, 2)
        elif result_kind == "stable":
            pages[1] = (
                [{**candidate, "video_id": "last", "file_name": "other.mp4"}],
                2,
                101,
                2,
            )
        elif result_kind == "legacy":
            with Session(database_engine) as db, db.begin():
                current = db.get(MaterialAssetOperation, op.id)
                current.remote_response = {
                    **current.remote_response,
                    "search_page": 2,
                    "search_total": 2,
                    "candidates": [candidate],
                }
            pages = [
                (
                    [{**candidate, "video_id": "last", "file_name": "other.mp4"}],
                    2,
                    101,
                    2,
                )
            ]
        elif result_kind == "bounded":
            pages = [(first, 1, 10001, 101)]
    elif result_kind == "unknown_count":
        pages = [([candidate], 1, None, 1)]
    elif result_kind == "multiple":
        pages = [([candidate, {**candidate, "video_id": "second-actual-id"}], 1, 2, 1)]
    else:
        pages = [([], 1, 0, 0)]
    for rows, page, count, total_pages in pages:
        data = {
            "list": rows,
            "page_info": {
                "page": page,
                "page_size": 100,
                "total_number": count,
                "total_page": total_pages,
            },
        }
        gateway_wire["sdk_data"]["data"] = data
        gateway_wire["wire"].results["file_video_ad_search"].append(
            {"content": [], "structuredContent": {"code": 0, "data": data}}
        )
        run(env, redis_client, kind="verify", operation_id=op.id)
    op = operation(env)
    if result_kind == "stable":
        assert (
            op.status == "verifying"
            and op.remote_response["video_id"] == "actual-source-vid"
        )
    else:
        assert op.status == "result_unknown" and not op.remote_response.get("video_id")
    if result_kind in {"changed_total", "duplicate", "legacy"}:
        assert op.remote_response.get("search_page") == 1
        assert op.remote_response.get("candidates") == []
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == len(pages) + 1
    with Session(database_engine) as db:
        assert_reserved(db, env, op.id)
