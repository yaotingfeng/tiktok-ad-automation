"""同一账户有界连续分页；真实PG/Redis，替身仅在HTTP边界。"""

from datetime import timedelta

import pytest

from app.modules.materials import cover_sharing, covers
from tests.modules.materials.test_cover_throughput import (
    image,
    incomplete_mid_page,
    matrix,
)
from tests.modules.materials.test_covers import job_state, run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def inventory(indices, number, size=100, total=156):
    return {
        "list": [image(i, target_id=True) for i in indices],
        "page_info": {
            "page": number,
            "page_size": size,
            "total_page": (total + size - 1) // size,
            "total_number": total,
        },
    }


def prepare_full_scan(source_env, redis_client, wire, identity):
    wire[1].extend([{"list": [image(0)]}, incomplete_mid_page()])
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    assert not [call for call in wire[0] if call[0] == "POST"]


def test_contiguous_account_scan_repairs_overlap_in_one_bounded_session(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    prepare_full_scan(source_env, redis_client, wire, identity)
    wire[1].extend(
        [
            inventory(range(1, 101), 1),
            inventory(range(98, 154), 2),
            inventory(range(1, 72), 1, 71),
            inventory(range(86, 157), 2, 71),
            {"failed_infos": {}},
        ]
    )
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    searches = [call for call in wire[0] if "image/ad/search" in call[1]]
    assert len(searches) == 5  # 一次不完整MID探测，随后同会话连续四页。


def test_scan_slice_stops_after_four_pages_with_durable_cursor(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]
    prepare_full_scan(source_env, redis_client, wire, identity)
    for number in range(1, 5):
        wire[1].append(
            inventory(
                range((number - 1) * 100 + 1, number * 100 + 1), number, total=900
            )
        )
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.cover_models import MaterialCoverShareBatch

    with Session(engine) as db:
        batch = db.get(MaterialCoverShareBatch, job_state(identity).share_batch_id)
        assert batch.scan_state["target-0"]["page"] == 5
        assert len(batch.scan_state["target-0"]["seen"]) == 400
    assert not [call for call in wire[0] if call[0] == "POST"]


@pytest.mark.parametrize("remaining", [12, 19])
def test_scan_slice_yields_before_budget_exhaustion_without_sending(
    source_env, redis_client, wire, monkeypatch, remaining
):
    identity = matrix(source_env, 1, 1)[0]
    prepare_full_scan(source_env, redis_client, wire, identity)
    original = covers._now

    def late_page():
        # 只推进业务时钟，模拟已用掉的任务预算；网络/状态逻辑照常执行。
        instant = original() + timedelta(seconds=40 - remaining)
        monkeypatch.setattr(cover_sharing.covers, "_now", lambda: instant)
        return inventory(range(1, 101), 1)

    wire[1].append(late_page)
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    assert len([call for call in wire[0] if "image/ad/search" in call[1]]) == 2
    assert not [call for call in wire[0] if call[0] == "POST"]


def test_complete_census_defers_write_when_budget_is_low_then_sends_once(
    source_env, redis_client, wire, monkeypatch
):
    identity = matrix(source_env, 1, 1)[0]
    prepare_full_scan(source_env, redis_client, wire, identity)
    original = covers._now

    def late_complete_page():
        instant = original() + timedelta(seconds=28)
        monkeypatch.setattr(cover_sharing.covers, "_now", lambda: instant)
        return inventory(range(1, 101), 1, total=100)

    wire[1].append(late_complete_page)
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    assert not [call for call in wire[0] if call[0] == "POST"]
    monkeypatch.setattr(cover_sharing.covers, "_now", original)
    wire[1].append({"failed_infos": {}})
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    assert len([call for call in wire[0] if "image/ad/search" in call[1]]) == 2
