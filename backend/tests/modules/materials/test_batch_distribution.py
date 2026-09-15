"""真实 PG/Redis 和官方 SDK/MCP HTTP 边界验证批次，不模拟业务网关。"""

import json
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.modules.materials.models import MaterialFile
from tests.integrations.tiktok.gateway_support import business_calls
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, target
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def database_engine(isolated_strategy_database, monkeypatch):
    engine = isolated_strategy_database[0]
    monkeypatch.setattr("tests.modules.materials.test_distribution.engine", engine)
    return engine


@pytest.fixture
def retain_build_history(isolated_strategy_database):
    return isolated_strategy_database[0]


@pytest.fixture
def share_case(gateway_case, database_engine, monkeypatch):
    from app.core.config import settings
    from app.integrations.tiktok.admission import PROTOCOL_OPERATIONS

    context, route, source = gateway_case
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                operation: {"lease_ms": 970000}
                for operation in {*PROTOCOL_OPERATIONS, "materials.share_assets"}
            },
        },
    )
    with Session(database_engine) as db, db.begin():
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name="shared.mp4",
            object_key=f"synthetic/{uuid4()}",
            byte_size=120,
            video_md5="a" * 32,
            storage_state="unavailable",
        )
        db.add(material)
        db.flush()
        env = {
            "context": context,
            "connection_id": route.connection_id,
            "bc_id": route.bc_id,
            "material_id": material.id,
            "source": source,
        }
        env["target"] = target(db, env, advertiser_id="90071992547409932")
        asset(db, env, source)
    return env


def seed_rectangle(env, database_engine, *, materials, targets):
    accounts = [env["target"]]
    material_envs = [env]
    with Session(database_engine) as db, db.begin():
        for index in range(1, targets):
            accounts.append(target(db, env, advertiser_id=f"900719925475{index:05}"))
        for index in range(1, materials):
            original = db.get(MaterialFile, env["material_id"])
            material = MaterialFile(
                **(
                    original.model_dump()
                    | {
                        "id": uuid4(),
                        "object_key": f"synthetic/{uuid4()}",
                        "file_name": f"shared-{index}.mp4",
                    }
                )
            )
            db.add(material)
            db.flush()
            item = {**env, "material_id": material.id}
            source = asset(db, item, env["source"])
            source.video_id = f"source-video-{index}"
            material_envs.append(item)
    tasks = [
        queue(item, account).task_id for item in material_envs for account in accounts
    ]
    rows = [
        {
            "video_id": f"vid-{env['source']}"
            if index == 0
            else f"source-video-{index}",
            "material_id": str(1234567890123456789 + index),
            "file_name": "shared.mp4" if index == 0 else f"shared-{index}.mp4",
            "signature": "a" * 32,
            "displayable": True,
        }
        for index in range(materials)
    ]
    return tasks, rows


def prepare_wire(wire, rows, *, requests=1):
    from app.integrations.tiktok.mcp.protocol import load_tool_contracts

    names = {c.operation: c.tool_name for c in load_tool_contracts()}
    wire["sdk_data"]["data"] = {"list": rows}
    for _ in range(requests):
        wire["wire"].results[names["materials.get_videos"]].append(
            {"content": [], "structuredContent": {"code": 0, "data": {"list": rows}}}
        )
        wire["wire"].results[names["materials.share_assets"]].append(
            {"content": [], "structuredContent": {"code": 0, "data": {}}}
        )
    return names


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_complete_twenty_by_ten_rectangle_sends_once(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
    record_property,
):
    from time import monotonic

    from sqlalchemy import event

    tasks, rows = seed_rectangle(share_case, database_engine, materials=20, targets=10)
    names = prepare_wire(gateway_wire, rows)
    selects = []

    def statement(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("SELECT"):
            selects.append(statement)

    event.listen(database_engine, "before_cursor_execute", statement)
    started = monotonic()
    run(share_case, redis_client, tasks[0], kind="prepare")
    elapsed = monotonic() - started
    event.remove(database_engine, "before_cursor_execute", statement)
    record_property("batch_worker_seconds", elapsed)
    record_property("batch_worker_selects", len(selects))
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 200, state(
        tasks[0]
    )[1].remote_response
    channel = gateway_case[1].channel
    calls = business_calls(gateway_wire, channel)
    if channel == "OFFICIAL_MCP":
        shares = [
            c["params"]["arguments"]
            for c in calls
            if c["params"]["name"] == names["materials.share_assets"]
        ]
    else:
        shares = [json.loads(c[2]["body"]) for c in calls if "/share/" in c[1]]
    assert len(shares) == 1
    assert len(shares[0]["material_ids"]) == 20
    assert len(shares[0]["shared_advertiser_ids"]) == 10
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert len(business_calls(gateway_wire, channel)) == 2


@pytest.mark.parametrize("total", [100, 1000, 10000])
def test_rectangular_partition_never_duplicates_or_invents_members(total):
    from app.modules.materials.batch_distribution import rectangle

    pending = {(str(index // 10), str(index % 10)) for index in range(total)}
    observed = set()
    count = 0
    while pending:
        selected = rectangle(pending, min(pending))
        assert selected <= pending
        assert not selected & observed
        assert len({row[0] for row in selected}) <= 20
        assert len({row[1] for row in selected}) <= 10
        pending -= selected
        observed |= selected
        count += 1
    assert len(observed) == total
    assert count == {100: 1, 1000: 5, 10000: 50}[total]


def test_twenty_one_by_eleven_partitions_into_four_exact_rectangles():
    from app.modules.materials.batch_distribution import rectangle

    pending = {
        (str(material), str(target)) for material in range(21) for target in range(11)
    }
    sizes = []
    while pending:
        selected = rectangle(pending, min(pending))
        assert selected <= pending
        sizes.append(len(selected))
        pending -= selected
    assert sorted(sizes) == [1, 10, 20, 200]


def test_sparse_rectangle_does_not_share_unrequested_target():
    from app.modules.materials.batch_distribution import rectangle

    selected = rectangle({("a", "1"), ("a", "2"), ("b", "1")}, ("a", "1"))
    assert len(selected) == 2
    assert ("b", "2") not in selected


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_known_target_ids_are_verified_together_and_missing_peer_alone_retries(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
):
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )

    tasks, rows = seed_rectangle(share_case, database_engine, materials=3, targets=1)
    with Session(database_engine) as db, db.begin():
        for index, task in enumerate(tasks):
            dist = db.get(MaterialDistribution, task)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            dist.status = "verifying"
            op.status = "verifying"
            op.remote_response = {
                **op.remote_response,
                "video_id": f"target-video-{index}",
            }
            rows[index]["video_id"] = f"target-video-{index}"
    prepare_wire(gateway_wire, rows[:2])
    run(share_case, redis_client, tasks[0])
    assert [state(task)[0].status for task in tasks] == ["ready", "ready", "verifying"]
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1
    prepare_wire(gateway_wire, rows[2:])
    run(share_case, redis_client, tasks[2])
    assert state(tasks[2])[0].status == "ready"
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 2


def test_unknown_batch_never_resends_any_member(
    share_case, database_engine, gateway_wire, redis_client
):
    from sqlmodel import select

    from app.modules.materials.batch_models import MaterialShareBatchReceipt

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    names = prepare_wire(gateway_wire, rows)
    gateway_wire["wire"].disconnect_after_accept(names["materials.share_assets"])
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["result_unknown"] * 4
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 2
    with Session(database_engine) as db:
        receipts = db.exec(select(MaterialShareBatchReceipt)).all()
        assert [r.effect for r in receipts] == ["UNKNOWN"]


@pytest.mark.parametrize("change", ["target_upload", "claim", "digest", "expiry"])
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_batch_rechecks_every_member_before_share(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
    gateway_case,
    change,
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )
    from tests.modules.accounts.test_material_gateway import after_material_http

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows)
    replacement = uuid4()

    def mutate():
        with Session(database_engine) as db, db.begin():
            dist = db.get(MaterialDistribution, tasks[-1])
            op = db.get(MaterialAssetOperation, dist.operation_id)
            if change == "claim":
                op.attempt_token = replacement
            elif change == "expiry":
                from datetime import UTC, datetime, timedelta

                op.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
            elif change == "digest":
                db.get(MaterialFile, dist.material_id).video_md5 = "b" * 32
            else:
                db.get(
                    BCAccountAccess,
                    (
                        dist.tenant_id,
                        dist.bc_id,
                        dist.advertiser_id,
                        share_case["connection_id"],
                    ),
                ).can_upload = False

    after_material_http(monkeypatch, gateway_case[1].channel, mutate)
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 1
    assert all(not state(task)[1].remote_response.get("send_armed") for task in tasks)
    if change == "claim":
        assert state(tasks[-1])[1].attempt_token == replacement
    if change == "digest":
        assert [state(task)[0].status for task in tasks] == [
            "queued",
            "queued",
            "blocked",
            "blocked",
        ]
    if change == "expiry":
        assert [state(task)[0].status for task in tasks] == ["queued"] * 4


def test_whole_batch_not_sent_receipt_allows_one_fresh_batch(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    from sqlmodel import select

    from app.core.config import settings
    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.materials.batch_models import MaterialShareBatchReceipt

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows, requests=2)
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                **settings.TIKTOK_CALL_POLICIES["endpoints"],
                "materials.share_assets": {
                    "lease_ms": 970000,
                    "endpoint_max_inflight": 1,
                },
            },
        },
    )
    scope = {
        "app_scope": f"official-mcp:{settings.MCP_SERVICE_QUOTA_SCOPE}",
        "endpoint": "materials.share_assets",
        "tenant_id": share_case["context"].tenant_id,
        "advertiser_id": share_case["source"],
        "lease_id": uuid4(),
    }
    assert admit_call(
        redis_client, **scope, policy=admission_policy("materials.share_assets")
    ).granted
    try:
        run(share_case, redis_client, tasks[0], kind="prepare")
        assert [state(task)[0].status for task in tasks] == ["queued"] * 4
        assert all(
            not state(task)[1].remote_response.get("send_armed") for task in tasks
        )
    finally:
        release_call(redis_client, **scope)
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 4
    with Session(database_engine) as db:
        receipts = db.exec(
            select(MaterialShareBatchReceipt).order_by(
                MaterialShareBatchReceipt.created_at
            )
        ).all()
        assert [r.effect for r in receipts] == ["NOT_SENT", "ACKNOWLEDGED"]
        assert receipts[0].batch_id != receipts[1].batch_id
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 3


def test_frozen_batch_members_and_receipts_are_immutable(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    mutations = [
        "UPDATE material_share_batch SET request_digest = repeat('b',64)",
        "UPDATE material_share_batch SET material_ids = '[\"different\"]'::jsonb",
        "UPDATE material_share_batch_member SET operation_claim = gen_random_uuid()",
        "UPDATE material_share_batch_member SET source_evidence = '{}'::jsonb",
        "UPDATE material_share_batch_receipt SET effect = 'NOT_SENT'",
        "DELETE FROM material_share_batch_receipt",
    ]
    with Session(database_engine) as db:
        for mutation in mutations:
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(text(mutation))


def test_duplicate_worker_deliveries_share_one_batch(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows)
    barrier = Barrier(2)

    def worker(task):
        barrier.wait(timeout=5)
        run(share_case, redis_client, task, kind="prepare")

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(worker, task) for task in (tasks[0], tasks[-1])]
        for job in jobs:
            job.result(timeout=40)
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 4
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 2


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_expired_prearm_batch_records_not_sent_and_recovers_original_claims(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    from datetime import UTC, datetime, timedelta

    from app.modules.materials.models import MaterialAssetOperation
    from tests.modules.accounts.test_material_gateway import after_material_http

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows, requests=2)

    def kill_after_source_read():
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        after_material_http(patch, "OFFICIAL_API", kill_after_source_read)
        with pytest.raises(KeyboardInterrupt):
            run(share_case, redis_client, tasks[0], kind="prepare")
    operation = state(tasks[0])[1]
    run(
        share_case,
        redis_client,
        tasks[0],
        kind="prepare",
        operation_id=operation.id,
        recovery_claim_id=operation.attempt_token,
    )
    assert state(tasks[0])[1].attempt_token == operation.attempt_token
    with Session(database_engine) as db, db.begin():
        for task in tasks:
            db.get(MaterialAssetOperation, state(task)[1].id).claimed_until = (
                datetime.now(UTC) - timedelta(seconds=1)
            )
    run(
        share_case,
        redis_client,
        tasks[0],
        kind="prepare",
        operation_id=operation.id,
        recovery_claim_id=operation.attempt_token,
    )
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 4
    assert len(business_calls(gateway_wire, "OFFICIAL_API")) == 3


def test_missing_source_only_blocks_missing_members_and_releases_others(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows[:1], requests=2)
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == [
        "queued",
        "queued",
        "blocked",
        "blocked",
    ]
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == [
        "verifying",
        "verifying",
        "blocked",
        "blocked",
    ]
    assert len(business_calls(gateway_wire, "OFFICIAL_MCP")) == 3


def selective_sdk_wire(monkeypatch, wire, rows, share_barrier=None, read_barrier=None):
    from urllib3.response import HTTPResponse

    def request(_pool, method, url, **kwargs):
        wire["sdk_calls"].append((method, url, kwargs))
        if method == "GET":
            ids = json.loads(dict(kwargs["fields"])["video_ids"])
            data = {"list": [row for row in rows if row["video_id"] in ids]}
            if read_barrier:
                read_barrier.wait(timeout=30)
        else:
            data = {}
            if share_barrier:
                share_barrier.wait(timeout=30)
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": data, "request_id": "synthetic-batch"}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_real_twenty_one_by_eleven_sends_four_exact_share_requests(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=21, targets=11)
    selective_sdk_wire(monkeypatch, gateway_wire, rows)
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 231
    bodies = [
        json.loads(call[2]["body"])
        for call in gateway_wire["sdk_calls"]
        if call[0] == "POST"
    ]
    assert len(bodies) == 4
    actual = []
    for body in bodies:
        actual.extend(
            (mid, account)
            for mid in body["material_ids"]
            for account in body["shared_advertiser_ids"]
        )
    assert len(actual) == len(set(actual)) == 231


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_two_full_batches_reach_share_http_concurrently_without_overlapping_members(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    tasks, rows = seed_rectangle(share_case, database_engine, materials=40, targets=10)
    selective_sdk_wire(monkeypatch, gateway_wire, rows, share_barrier=Barrier(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, share_case, redis_client, task, kind="prepare")
            for task in (tasks[0], tasks[-1])
        ]
        for future in futures:
            future.result(timeout=100)
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 400
    bodies = [
        json.loads(call[2]["body"])
        for call in gateway_wire["sdk_calls"]
        if call[0] == "POST"
    ]
    assert len(bodies) == 2
    assert all(
        len(body["material_ids"]) == 20 and len(body["shared_advertiser_ids"]) == 10
        for body in bodies
    )
    assert not set(bodies[0]["material_ids"]) & set(bodies[1]["material_ids"])


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_two_targets_verify_shared_materials_concurrently_in_material_lock_order(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )

    tasks, rows = seed_rectangle(share_case, database_engine, materials=4, targets=2)
    with Session(database_engine) as db, db.begin():
        for index, task in enumerate(tasks):
            dist = db.get(MaterialDistribution, task)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            dist.status = op.status = "verifying"
            op.remote_response = {
                **op.remote_response,
                "video_id": f"target-video-{index // 2}",
            }
        for index, row in enumerate(rows):
            row["video_id"] = f"target-video-{index}"
    selective_sdk_wire(monkeypatch, gateway_wire, rows, read_barrier=Barrier(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, share_case, redis_client, task) for task in tasks[:2]
        ]
        for future in futures:
            future.result(timeout=40)
    assert [state(task)[0].status for task in tasks] == ["ready"] * 8
    assert len(gateway_wire["sdk_calls"]) == 2


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_armed_expired_batch_appends_unknown_and_never_repeats_share(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    import os
    import signal
    from datetime import UTC, datetime, timedelta

    import urllib3
    from sqlmodel import select

    from app.modules.materials.batch_models import MaterialShareBatchReceipt
    from app.modules.materials.models import MaterialAssetOperation

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows)
    request = urllib3.PoolManager.request

    def kill_after_share(pool, method, url, **kwargs):
        result = request(pool, method, url, **kwargs)
        if "/share/" in url:
            # SDK 共享在其线程池执行；由操作系统中断主 worker，避免只杀线程令 SDK 永久等结果。
            os.kill(os.getpid(), signal.SIGINT)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(urllib3.PoolManager, "request", kill_after_share)
        with pytest.raises(KeyboardInterrupt):
            run(share_case, redis_client, tasks[0], kind="prepare")
    original = state(tasks[0])[1]
    with Session(database_engine) as db, db.begin():
        for task in tasks:
            db.get(MaterialAssetOperation, state(task)[1].id).claimed_until = (
                datetime.now(UTC) - timedelta(seconds=1)
            )
    run(
        share_case,
        redis_client,
        tasks[0],
        kind="prepare",
        operation_id=original.id,
        recovery_claim_id=original.attempt_token,
    )
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["result_unknown"] * 4
    with Session(database_engine) as db:
        assert [
            row.effect for row in db.exec(select(MaterialShareBatchReceipt)).all()
        ] == ["UNKNOWN"]
    assert len(gateway_wire["sdk_calls"]) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_changed_digest_after_bulk_target_response_cannot_publish_ready(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
    gateway_case,
):
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )
    from tests.modules.accounts.test_material_gateway import after_material_http

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    with Session(database_engine) as db, db.begin():
        for index, task in enumerate(tasks):
            dist = db.get(MaterialDistribution, task)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            dist.status = op.status = "verifying"
            op.remote_response = {
                **op.remote_response,
                "video_id": rows[index]["video_id"],
            }
    prepare_wire(gateway_wire, rows)

    def change_digest():
        with Session(database_engine) as db, db.begin():
            db.get(MaterialFile, state(tasks[0])[0].material_id).video_md5 = "b" * 32

    after_material_http(monkeypatch, gateway_case[1].channel, change_digest)
    run(share_case, redis_client, tasks[0])
    assert state(tasks[0])[2] is None
    assert state(tasks[0])[0].status != "ready"
    assert state(tasks[1])[0].status == "ready"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_all_verified_members_close_batch_without_rewriting_share_receipt(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    monkeypatch,
):
    from sqlmodel import select

    from app.modules.materials.batch_models import (
        MaterialShareBatch,
        MaterialShareBatchReceipt,
    )
    from app.modules.materials.models import MaterialAssetOperation

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    selective_sdk_wire(monkeypatch, gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    with Session(database_engine) as db, db.begin():
        for index, task in enumerate(tasks):
            op = db.get(MaterialAssetOperation, state(task)[1].id)
            op.remote_response = {
                **op.remote_response,
                "video_id": rows[index // 2]["video_id"],
            }
    run(share_case, redis_client, tasks[0])
    run(share_case, redis_client, tasks[1])
    with Session(database_engine) as db:
        assert db.exec(select(MaterialShareBatch)).one().status == "completed"
        assert [
            receipt.effect
            for receipt in db.exec(select(MaterialShareBatchReceipt)).all()
        ] == ["ACKNOWLEDGED"]
    assert [state(task)[0].status for task in tasks] == ["ready"] * 4


def test_foreign_tenant_or_actor_cannot_claim_existing_batch_candidates(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
):
    from app.core.context import TenantContext
    from app.core.errors import DomainError
    from app.modules.materials.distribution import run_distribution

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=2)
    prepare_wire(gateway_wire, rows)
    original = share_case["context"]
    for tenant, actor in ((uuid4(), original.actor_id), (original.tenant_id, uuid4())):
        with pytest.raises(DomainError):
            run_distribution(
                database_engine=database_engine,
                redis_client=redis_client,
                context=TenantContext(
                    tenant_id=tenant, actor_id=actor, role="operator"
                ),
                distribution_id=tasks[0],
                kind="prepare",
            )
    assert [state(task)[0].status for task in tasks] == ["queued"] * 4
    assert not gateway_wire["wire"].calls
