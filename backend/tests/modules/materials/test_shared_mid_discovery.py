"""共享 ACK 后按源 MID 批量发现真实目标 VID，只替换 SDK/MCP HTTP 边界。"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session, select

from app.modules.materials.batch_models import MaterialShareBatchReceipt
from app.modules.materials.models import AccountMaterial, MaterialUploadAttempt
from tests.integrations.tiktok.gateway_support import business_calls
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


def search_wire(wire, names, rows, *, total_page=1, total_number=None):
    data = {
        "list": rows,
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_page": total_page,
            "total_number": len(rows) if total_number is None else total_number,
        },
    }
    wire["sdk_data"]["data"] = data
    wire["wire"].results[names["materials.search_videos"]].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )


def search_calls(wire, channel, names):
    calls = business_calls(wire, channel)
    if channel == "OFFICIAL_MCP":
        return [
            call["params"]["arguments"]
            for call in calls
            if call["params"]["name"] == names["materials.search_videos"]
        ]
    results = []
    for _method, url, options in calls:
        if "/video/ad/search/" not in url:
            continue
        args = dict(options.get("fields", {}))
        args.update(
            {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        )
        if isinstance(args.get("filtering"), str):
            args["filtering"] = json.loads(args["filtering"])
        results.append(args)
    return results


def target_records(rows, *, account="target"):
    return [
        {
            **row,
            "video_id": f"actual-{account}-{index}",
            "size": 120,
            "width": 720,
            "height": 1280,
            "duration": 12.5,
            "format": "mp4",
        }
        for index, row in enumerate(rows)
    ]


def assert_single_share(database_engine):
    # 核实缺项或异常只能继续读取，不能产生第二次物理共享或上传。
    with Session(database_engine) as db:
        receipts = db.exec(select(MaterialShareBatchReceipt)).all()
        assert len(receipts) == 1 and receipts[0].effect == "ACKNOWLEDGED"
        assert db.exec(select(MaterialUploadAttempt)).all() == []


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_twenty_shared_mids_discover_actual_target_vids_in_one_read(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=20, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    assert [state(task)[0].status for task in tasks] == ["verifying"] * 20
    target_rows = target_records(rows)
    search_wire(gateway_wire, names, target_rows)
    run(share_case, redis_client, tasks[0])
    assert [state(task)[0].status for task in tasks] == ["ready"] * 20
    assert [state(task)[2].video_id for task in tasks] == [
        row["video_id"] for row in target_rows
    ]
    calls = search_calls(gateway_wire, gateway_case[1].channel, names)
    assert len(calls) == 1
    assert set(calls[0]["filtering"]["material_ids"]) == {
        row["material_id"] for row in rows
    }
    assert "video_name" not in calls[0]["filtering"]
    before = len(business_calls(gateway_wire, gateway_case[1].channel))
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
        run(share_case, redis_client, task)
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == before == 3
    assert_single_share(database_engine)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_mid_retries_only_that_member_without_resharing(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=3, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    target_rows = target_records(rows)
    search_wire(gateway_wire, names, target_rows[:2])
    run(share_case, redis_client, tasks[0])
    assert [state(task)[0].status for task in tasks] == ["ready", "ready", "verifying"]
    before = [state(task)[2].model_dump() for task in tasks[:2]]
    search_wire(gateway_wire, names, target_rows[2:])
    run(share_case, redis_client, tasks[2])
    assert state(tasks[2])[0].status == "ready"
    assert state(tasks[2])[2].video_id == target_rows[2]["video_id"]
    assert [state(task)[2].model_dump() for task in tasks[:2]] == before
    calls = search_calls(gateway_wire, gateway_case[1].channel, names)
    assert len(calls) == 2
    assert calls[-1]["filtering"] == {"material_ids": [rows[2]["material_id"]]}
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 4
    assert_single_share(database_engine)


@pytest.mark.parametrize(
    "malformed",
    [
        "unrequested_mid",
        "duplicate_mid",
        "wrong_account",
        "partial_page",
        "missing_total",
    ],
)
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_ambiguous_or_out_of_scope_page_never_publishes_ready(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
    malformed,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    target_rows = target_records(rows)
    page_options = {}
    if malformed == "unrequested_mid":
        target_rows = [
            {**row, "material_id": f"987654321012345678{index}"}
            for index, row in enumerate(target_rows)
        ]
    elif malformed == "duplicate_mid":
        target_rows[1]["material_id"] = target_rows[0]["material_id"]
    elif malformed == "wrong_account":
        target_rows = [
            {**row, "advertiser_id": share_case["source"]} for row in target_rows
        ]
    elif malformed == "partial_page":
        page_options = {"total_page": 2, "total_number": 101}
    search_wire(gateway_wire, names, target_rows, **page_options)
    if malformed == "missing_total":
        gateway_wire["sdk_data"]["data"]["page_info"].pop("total_number")
    run(share_case, redis_client, tasks[0])
    assert all(state(task)[0].status != "ready" for task in tasks)
    assert all(state(task)[2] is None for task in tasks)
    assert len(search_calls(gateway_wire, gateway_case[1].channel, names)) == 1
    for task in tasks:
        run(share_case, redis_client, task, kind="prepare")
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 3
    assert_single_share(database_engine)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_twenty_by_ten_shared_rectangle_needs_only_ten_scoped_searches(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=20, targets=10)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    for account_index in range(10):
        account_tasks = tasks[account_index::10]
        advertiser = state(account_tasks[0])[0].advertiser_id
        target_rows = target_records(rows, account=advertiser)
        search_wire(gateway_wire, names, target_rows)
        run(share_case, redis_client, account_tasks[0])
        assert [state(task)[0].status for task in account_tasks] == ["ready"] * 20
        assert [state(task)[2].video_id for task in account_tasks] == [
            row["video_id"] for row in target_rows
        ]
        for other_index in range(account_index + 1, 10):
            assert [state(task)[0].status for task in tasks[other_index::10]] == [
                "verifying"
            ] * 20
        calls = search_calls(gateway_wire, gateway_case[1].channel, names)
        assert len(calls) == account_index + 1
        assert calls[-1]["advertiser_id"] == advertiser
        assert set(calls[-1]["filtering"]["material_ids"]) == {
            row["material_id"] for row in rows
        }
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 12
    assert_single_share(database_engine)
    with Session(database_engine) as db:
        sources = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.advertiser_id == share_case["source"]
            )
        ).all()
        assert {source.video_id for source in sources} == {
            row["video_id"] for row in rows
        }


@pytest.mark.parametrize(
    "field,value", [("signature", "b" * 32), ("size", 121), ("file_name", "other.mp4")]
)
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_mid_match_without_frozen_content_match_cannot_publish(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
    field,
    value,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    target_rows = target_records(rows)
    target_rows[1][field] = value
    search_wire(gateway_wire, names, target_rows)
    run(share_case, redis_client, tasks[0])
    assert state(tasks[0])[0].status == "ready"
    assert state(tasks[1])[0].status == "verifying"
    assert state(tasks[1])[2] is None
    assert len(search_calls(gateway_wire, gateway_case[1].channel, names)) == 1
    assert_single_share(database_engine)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_size_keeps_actual_vid_but_requires_strict_detail_read(
    share_case,
    database_engine,
    gateway_wire,
    redis_client,
    gateway_case,
):
    tasks, rows = seed_rectangle(share_case, database_engine, materials=2, targets=1)
    names = prepare_wire(gateway_wire, rows)
    run(share_case, redis_client, tasks[0], kind="prepare")
    target_rows = target_records(rows)
    target_rows[1].pop("size")
    search_wire(gateway_wire, names, target_rows)
    run(share_case, redis_client, tasks[0])
    assert state(tasks[0])[0].status == "ready"
    assert state(tasks[1])[0].status == "verifying"
    assert state(tasks[1])[2] is None
    assert state(tasks[1])[1].remote_response["video_id"] == target_rows[1]["video_id"]
    # 第二次缺尺寸的详情不能把合成的本地尺寸当平台证据。
    prepare_wire(gateway_wire, target_rows[1:])
    run(share_case, redis_client, tasks[1])
    assert state(tasks[1])[0].status == "verifying"
    assert state(tasks[1])[2] is None
    target_rows[1]["size"] = 120
    prepare_wire(gateway_wire, target_rows[1:])
    run(share_case, redis_client, tasks[1])
    assert state(tasks[1])[0].status == "ready"
    assert state(tasks[1])[2].video_id == target_rows[1]["video_id"]
    assert len(search_calls(gateway_wire, gateway_case[1].channel, names)) == 1
    assert len(business_calls(gateway_wire, gateway_case[1].channel)) == 5
    assert_single_share(database_engine)
