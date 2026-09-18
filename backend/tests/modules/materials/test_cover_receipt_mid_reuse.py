"""图片上传已保存真实 MID 后，共享直接使用回执，不重复读取源图片。"""

import pytest
from sqlmodel import Session

from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from tests.modules.materials.test_channel_covers import (
    app_config as app_config,
)
from tests.modules.materials.test_channel_covers import (
    cover_env as cover_env,
)
from tests.modules.materials.test_channel_covers import (
    database_engine as database_engine,
)
from tests.modules.materials.test_channel_covers import (
    gateway_case as gateway_case,
)
from tests.modules.materials.test_channel_covers import (
    gateway_wire as gateway_wire,
)
from tests.modules.materials.test_channel_covers import (
    image_data,
    job,
    prepare_replies,
    queue,
    run,
    video_data,
)
from tests.modules.materials.test_channel_covers import (
    policy as policy,
)
from tests.modules.materials.test_cover_fast_path import (
    build_queue,
    complete_receipt,
    search_data,
)
from tests.modules.materials.test_cover_fast_path import (
    shared_cover as shared_cover,
)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_complete_source_upload_without_optional_mid_is_ready_without_image_read(
    cover_env, gateway_wire, database_engine, redis_client
):
    identity = queue(cover_env, database_engine).task_id
    receipt = complete_receipt(cover_env)
    receipt.pop("material_id")
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            ("file_image_ad_upload", receipt),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert current.known_image_id == "actual-image-id"
    assert current.image_mid is None
    assert current.dispatch_id is None
    assert current.request_armed_at is not None
    assert build_queue(cover_env, database_engine).state == "ready"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id == (
            "actual-image-id"
        )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("source_mid", ["900001", None, "invalid-mid"])
def test_share_reuses_saved_mid_and_discovers_only_missing_or_invalid_mid(
    cover_env,
    shared_cover,
    gateway_wire,
    database_engine,
    redis_client,
    source_mid,
):
    with Session(database_engine) as db, db.begin():
        db.get(MaterialCoverJob, shared_cover).image_mid = source_mid
    identity = build_queue(cover_env, database_engine).task_id
    target = image_data("target-cover.jpg", image_id="actual-target-cover")
    steps = []
    if source_mid != "900001":
        steps.append(
            (
                "file_image_ad_info_get",
                image_data("source-cover.jpg", image_id="shared-image-id"),
            )
        )
    steps.extend(
        [
            ("file_image_ad_search", search_data([])),
            ("creative_asset_share_get", {"failed_infos": {}}),
        ]
    )
    prepare_replies(cover_env, gateway_wire, steps)
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "VERIFYING"
    assert current.request_armed_at is not None
    assert current.known_image_id is None

    # 原生 IMAGE 共享没有返回目标 image_id，仍需获取真实目标图片身份。
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_image_ad_search", search_data(target["list"]))],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert current.known_image_id == "actual-target-cover"
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id == (
            "actual-target-cover"
        )
