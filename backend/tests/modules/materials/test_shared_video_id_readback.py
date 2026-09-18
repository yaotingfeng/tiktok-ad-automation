"""共享列表索引缺失时，仍以目标账户 VID 详情实证完成核验。"""

import pytest
from sqlmodel import Session

from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.materials.test_batch_distribution import (
    database_engine as database_engine,
)
from tests.modules.materials.test_batch_distribution import prepare_wire, seed_rectangle
from tests.modules.materials.test_batch_distribution import (
    retain_build_history as retain_build_history,
)
from tests.modules.materials.test_batch_distribution import share_case as share_case
from tests.modules.materials.test_distribution import run, state
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


def target_record(row, advertiser_id):
    return {
        **row,
        "advertiser_id": advertiser_id,
        "size": 120,
        "width": 720,
        "height": 1280,
        "duration": 10,
        "format": "mp4",
    }


def reply(wire, names, operation, rows, *, search=False):
    data = {"list": rows}
    if search:
        data["page_info"] = {
            "page": 1,
            "page_size": 100,
            "total_page": 1 if rows else 0,
            "total_number": len(rows),
        }
    wire["wire"].results[names[operation]].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_acknowledged_share_reads_requested_vid_on_target_before_empty_list(
    share_case, database_engine, gateway_wire, redis_client
):
    # 若仅依赖 MID/名称列表，这个真实传输边界夹具将停在 verifying。
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert all(state(task)[1].remote_response["share_acknowledged"] for task in tasks)
    target_rows = [target_record(row, share_case["target"]) for row in rows]
    gateway_wire["sdk_data"]["data"] = {"list": target_rows}
    gateway_wire["wire"].results[names["materials.get_videos"]].append(
        {"content": [], "structuredContent": {"code": 0, "data": {"list": target_rows}}}
    )
    gateway_wire["wire"].results[names["materials.search_videos"]].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": {
                    "list": [],
                    "page_info": {
                        "page": 1,
                        "page_size": 100,
                        "total_page": 0,
                        "total_number": 0,
                    },
                },
            },
        }
    )
    run(share_case, redis_client, tasks[0])
    for task, row in zip(tasks, rows, strict=True):
        dist, operation, mapping = state(task)
        assert dist.status == "ready"
        assert operation.status == "succeeded"
        assert mapping.video_id == row["video_id"]
        assert mapping.advertiser_id == share_case["target"]
        assert operation.remote_response["video_id"] == row["video_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"signature": "b" * 32},
        {"size": 121},
        {"size": None},
        {"displayable": False},
        {"displayable": None},
        {"advertiser_id": "wrong-account"},
        {"video_id": "other-content-vid"},
    ],
)
def test_candidate_details_never_publish_unverified_identity(
    share_case, database_engine, gateway_wire, redis_client, change
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=1, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    reply(
        gateway_wire,
        names,
        "materials.get_videos",
        [target_record(rows[0], share_case["target"]) | change],
    )
    reply(gateway_wire, names, "materials.search_videos", [], search=True)
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert dist.status != "ready"
    assert operation.status != "succeeded"
    assert mapping is None
    assert "video_id" not in operation.remote_response


def test_missing_candidate_falls_back_to_actual_target_list_vid(
    share_case, database_engine, gateway_wire, redis_client
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=1, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    reply(gateway_wire, names, "materials.get_videos", [])
    reply(
        gateway_wire,
        names,
        "materials.search_videos",
        [
            target_record(rows[0], share_case["target"])
            | {"video_id": "actual-target-vid"}
        ],
        search=True,
    )
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert (dist.status, operation.status, mapping.video_id) == (
        "ready",
        "succeeded",
        "actual-target-vid",
    )


@pytest.mark.parametrize("guard", ["no_ack", "cross_bc", "source_scope"])
def test_unacknowledged_or_cross_bc_cannot_use_source_candidate(
    share_case, database_engine, gateway_wire, redis_client, guard
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=1, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    with Session(database_engine) as db, db.begin():
        dist = db.get(MaterialDistribution, tasks[0])
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        operation.remote_response = operation.remote_response | (
            {"share_acknowledged": False}
            if guard == "no_ack"
            else {"source_bc_id": "other-bc"}
            if guard == "source_scope"
            else {"source_bc_id": "other-bc", "transport": "url_relay"}
        )
    reply(
        gateway_wire,
        names,
        "materials.get_videos",
        [target_record(rows[0], share_case["target"])],
    )
    reply(gateway_wire, names, "materials.search_videos", [], search=True)
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert dist.status != "ready"
    assert mapping is None


def test_explicit_read_only_resets_stopped_budget_and_reads_only_anchor(
    share_case, database_engine, gateway_wire, redis_client
):
    from app.modules.materials.distribution import (
        _stop_reconciliation,
        queue_distribution,
    )

    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    with Session(database_engine) as db, db.begin():
        dist = db.get(MaterialDistribution, tasks[0])
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        _stop_reconciliation(dist, operation)
        original_operation = operation.id
    reply(
        gateway_wire,
        names,
        "materials.get_videos",
        [target_record(rows[0], share_case["target"])],
    )
    run(share_case, redis_client, tasks[0], read_only=True)
    assert state(tasks[0])[0].status == "result_unknown"
    with Session(database_engine) as db, db.begin():
        dist = db.get(MaterialDistribution, tasks[0])
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        queue_distribution(
            db, dist, operation, kind="verify", observe=True, read_only=True
        )
    run(share_case, redis_client, tasks[0], read_only=True)
    dist, operation, mapping = state(tasks[0])
    assert (dist.status, operation.id, mapping.video_id) == (
        "ready",
        original_operation,
        rows[0]["video_id"],
    )
    assert state(tasks[1])[0].status == "verifying"
    assert state(tasks[1])[2] is None


def test_partial_details_survive_other_members_search_failure(
    share_case, database_engine, gateway_wire, redis_client
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    reply(
        gateway_wire,
        names,
        "materials.get_videos",
        [target_record(rows[0], share_case["target"])],
    )
    gateway_wire["wire"].results[names["materials.search_videos"]].append(
        {
            "content": [],
            "structuredContent": {
                "code": 50000,
                "message": "synthetic-read-error",
                "data": {},
            },
        }
    )
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert (dist.status, operation.status, mapping.video_id) == (
        "ready",
        "succeeded",
        rows[0]["video_id"],
    )
    assert state(tasks[1])[0].status != "ready"
    assert state(tasks[1])[2] is None


def test_single_nonbatch_acknowledged_native_uses_target_vid_details(
    share_case, database_engine, gateway_wire, redis_client
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=1, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    with Session(database_engine) as db, db.begin():
        dist = db.get(MaterialDistribution, tasks[0])
        operation = db.get(MaterialAssetOperation, dist.operation_id)
        operation.remote_response = {
            key: value
            for key, value in operation.remote_response.items()
            if key != "share_batch_id"
        }
    reply(
        gateway_wire,
        names,
        "materials.get_videos",
        [target_record(rows[0], share_case["target"])],
    )
    reply(gateway_wire, names, "materials.search_videos", [], search=True)
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert (dist.status, operation.status) == ("ready", "succeeded")
    assert mapping.video_id == rows[0]["video_id"]


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize(
    "changed", ["source_video_id", "share_acknowledged", "source_bc_id", "transport"]
)
def test_late_target_details_cannot_publish_after_frozen_candidate_changes(
    share_case, database_engine, gateway_wire, redis_client, monkeypatch, changed
):
    import urllib3

    tasks, rows = seed_rectangle(share_case, database_engine, materials=1, targets=1)
    prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    gateway_wire["sdk_data"]["data"] = {
        "list": [target_record(rows[0], share_case["target"])]
    }
    original_request = urllib3.PoolManager.request

    def response_after_change(pool, method, url, **kwargs):
        response = original_request(pool, method, url, **kwargs)
        with Session(database_engine) as db, db.begin():
            dist = db.get(MaterialDistribution, tasks[0])
            operation = db.get(MaterialAssetOperation, dist.operation_id)
            operation.remote_response = operation.remote_response | {
                changed: False
                if changed == "share_acknowledged"
                else "changed-identity"
            }
        return response

    monkeypatch.setattr(urllib3.PoolManager, "request", response_after_change)
    run(share_case, redis_client, tasks[0])
    dist, operation, mapping = state(tasks[0])
    assert dist.status != "ready"
    assert operation.status != "succeeded"
    assert mapping is None
