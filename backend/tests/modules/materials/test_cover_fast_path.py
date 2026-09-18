"""完整封面回执与单任务会话：真实数据库/Redis，替身仅在官方 HTTP 边界。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.materials import covers
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
    post_count,
    prepare_replies,
    queue,
    run,
    video_data,
)
from tests.modules.materials.test_channel_covers import (
    policy as policy,
)


def complete_receipt(env):
    return {
        "advertiser_id": env["advertiser"],
        "image_id": "actual-image-id",
        "material_id": "1234567890123456789",
        "signature": "c" * 32,
        "width": 360,
        "height": 640,
        "displayable": False,
    }


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("complete", [False, True])
def test_cover_upload_uses_one_session_and_publishes_only_complete_receipt(
    cover_env, gateway_wire, database_engine, redis_client, complete
):
    identity = queue(cover_env, database_engine).task_id
    response = complete_receipt(cover_env)
    if not complete:
        response.pop("height")
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            ("file_image_ad_upload", response),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == ("READY" if complete else "VERIFYING")
    assert current.known_image_id == "actual-image-id"
    with Session(database_engine) as db:
        mapping = db.get(AccountMaterial, cover_env["asset_id"])
        assert mapping.image_id == ("actual-image-id" if complete else None)
    assert current.image_mid == ("1234567890123456789" if complete else None)
    if cover_env["route"].channel == "OFFICIAL_MCP":
        calls = gateway_wire["wire"].calls
        assert sum(c["method"] == "initialize" for c in calls) == 1
        assert sum(c["method"] == "tools/list" for c in calls) == 1
        assert [c["params"]["name"] for c in calls if c["method"] == "tools/call"] == [
            "file_video_ad_info_get",
            "file_image_ad_upload",
        ]
    else:
        assert len(gateway_wire["sdk_calls"]) == 2


@pytest.fixture
def shared_cover(cover_env, database_engine):
    env = cover_env
    with Session(database_engine) as db, db.begin():
        source = "90071992547409932"
        db.add(
            AdvertiserAccount(
                tenant_id=env["context"].tenant_id,
                advertiser_id=source,
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            )
        )
        db.flush()
        db.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id=env["route"].bc_id,
                advertiser_id=source,
                connection_id=env["route"].connection_id,
                in_bc=True,
                authorized=True,
                active=True,
                can_upload=True,
                can_build=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            )
        )
        asset = AccountMaterial(
            tenant_id=env["context"].tenant_id,
            bc_id=env["route"].bc_id,
            material_id=env["material_id"],
            advertiser_id=source,
            connection_id=env["route"].connection_id,
            video_id="source-content-vid",
            image_id="shared-image-id",
            status="available",
            verified_at=datetime.now(UTC),
        )
        db.add(asset)
        db.flush()
        cover = MaterialCoverJob(
            tenant_id=env["context"].tenant_id,
            bc_id=env["route"].bc_id,
            material_id=env["material_id"],
            asset_id=asset.id,
            advertiser_id=source,
            connection_id=env["route"].connection_id,
            actor_id=env["context"].actor_id,
            frozen_route=env["route"].model_dump(mode="json"),
            video_id=asset.video_id,
            video_md5="a" * 32,
            remote_name="source-cover.jpg",
            status="READY",
            request_armed_at=datetime.now(UTC),
            known_image_id=asset.image_id,
            signature="c" * 32,
            width=360,
            height=640,
        )
        db.add(cover)
        db.flush()
        return cover.id


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("height", None),
        ("width", True),
        ("displayable", "false"),
        ("signature", "bad"),
        ("height", 360),
    ],
)
def test_incomplete_or_malformed_upload_receipt_keeps_real_id_for_readback(
    cover_env, gateway_wire, database_engine, redis_client, field, value
):
    identity = queue(cover_env, database_engine).task_id
    response = complete_receipt(cover_env)
    response[field] = value
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_video_ad_info_get", video_data()), ("file_image_ad_upload", response)],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "VERIFYING" and current.known_image_id == "actual-image-id"
    assert current.request_armed_at
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("exhaust", [False, True])
def test_transient_known_image_read_retries_without_another_upload(
    cover_env, gateway_wire, database_engine, redis_client, monkeypatch, exhaust
):
    import json

    import httpx2

    from app.integrations.tiktok.mcp import transport

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
    run(cover_env, database_engine, redis_client, identity)
    original = job(database_engine, identity)
    assert original.status == "VERIFYING" and original.request_armed_at
    original_transport = transport._new_http_transport
    failures = []

    class OfflineRead(original_transport):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            if (
                message.get("method") == "tools/call"
                and message["params"]["name"] == "file_image_ad_info_get"
                and len(failures) < (3 if exhaust else 1)
            ):
                failures.append(True)
                raise httpx2.ReadError("synthetic image read interruption")
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", OfflineRead)
    for attempt in range(1, (3 if exhaust else 1) + 1):
        run(cover_env, database_engine, redis_client, identity, read=True)
        current = job(database_engine, identity)
        assert current.failure_count == attempt
        assert current.known_image_id == original.known_image_id
        assert current.request_armed_at == original.request_armed_at
        assert current.status == ("UNKNOWN" if attempt == 3 else "VERIFYING")
        assert post_count(cover_env, gateway_wire) == 1
    if not exhaust:
        prepare_replies(
            cover_env,
            gateway_wire,
            [("file_image_ad_info_get", image_data(original.remote_name))],
        )
        run(cover_env, database_engine, redis_client, identity, read=True)
        assert job(database_engine, identity).status == "READY"
        assert job(database_engine, identity).failure_count == 0
        assert post_count(cover_env, gateway_wire) == 1


def build_queue(env, database_engine):
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


def search_data(rows):
    return {
        "list": rows,
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_page": 1,
            "total_number": len(rows),
        },
    }


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "inventory",
    ["alias", "absent", "wrong_signature", "wrong_ratio", "wrong_dimensions"],
)
def test_source_candidate_requires_account_inventory_without_upload(
    cover_env, gateway_wire, database_engine, redis_client, inventory
):
    with Session(database_engine) as db, db.begin():
        db.get(AccountMaterial, cover_env["asset_id"]).image_id = "legacy-tos-id"
    identity = queue(cover_env, database_engine).task_id
    info = image_data("historical-image.jpg", image_id="legacy-tos-id")
    row = image_data("renamed.jpg", image_id="account-inventory-tos-id")["list"][0]
    if inventory == "wrong_signature":
        row["signature"] = "d" * 32
    elif inventory == "wrong_ratio":
        row["height"] = row["width"]
    elif inventory == "wrong_dimensions":
        row["width"], row["height"] = 720, 1280
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_video_ad_info_get", video_data()),
            ("file_image_ad_info_get", info),
            (
                "file_image_ad_search",
                search_data([] if inventory == "absent" else [row]),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    current = job(database_engine, identity)
    assert current.status == ("READY" if inventory == "alias" else "BLOCKED")
    assert current.request_armed_at is None
    assert post_count(cover_env, gateway_wire) == 0
    if inventory == "alias":
        assert current.image_mid == "900001"
        if cover_env["route"].channel == "OFFICIAL_MCP":
            request = next(
                c["params"]["arguments"]
                for c in gateway_wire["wire"].calls
                if c["method"] == "tools/call"
                and c["params"]["name"] == "file_image_ad_search"
            )
            assert request["filtering"] == {"material_ids": ["900001"]}
        else:
            import json

            request = next(
                dict(c[2]["fields"])
                for c in gateway_wire["sdk_calls"]
                if "/file/image/ad/search/" in c[1]
            )
            assert json.loads(request["filtering"]) == {"material_ids": ["900001"]}
    else:
        assert current.error_code == "cover_source_inventory_unverified"


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_legacy_build_candidate_promotion_only_queues_read(
    cover_env, gateway_wire, database_engine, redis_client
):
    identity = build_queue(cover_env, database_engine).task_id
    with Session(database_engine) as db, db.begin():
        row = db.get(MaterialCoverJob, identity)
        row.candidate_image_id, row.signature = "legacy-tos-id", "c" * 32
        row.width, row.height, row.status = 360, 640, "READY"
        row.dispatch_id = None
        db.get(AccountMaterial, row.asset_id).image_id = row.candidate_image_id
    promoted = queue(cover_env, database_engine)
    assert promoted.task_id == identity
    assert job(database_engine, identity).status == "VERIFYING"
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_image_ad_info_get", image_data("old.jpg", image_id="legacy-tos-id")),
            (
                "file_image_ad_search",
                search_data(image_data("renamed.jpg", image_id="alias-id")["list"]),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "READY"
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("already_present", [False, True])
def test_image_share_uses_real_mid_and_actual_target_id(
    cover_env,
    shared_cover,
    gateway_wire,
    database_engine,
    redis_client,
    already_present,
):
    assert shared_cover
    identity = build_queue(cover_env, database_engine).task_id
    source = image_data("source-cover.jpg", image_id="shared-image-id")
    target = image_data("renamed-target.jpg", image_id="different-target-image")
    steps = [
        ("file_image_ad_info_get", source),
        (
            "file_image_ad_search",
            search_data(target["list"] if already_present else []),
        ),
    ]
    if not already_present:
        steps.append(("creative_asset_share_get", {"failed_infos": {}}))
    prepare_replies(cover_env, gateway_wire, steps)
    run(cover_env, database_engine, redis_client, identity)
    if not already_present:
        assert job(database_engine, identity).status == "VERIFYING"
        prepare_replies(
            cover_env,
            gateway_wire,
            [("file_image_ad_search", search_data(target["list"]))],
        )
        run(cover_env, database_engine, redis_client, identity, read=True)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert current.known_image_id == "different-target-image"
    with Session(database_engine) as db:
        assert (
            db.get(AccountMaterial, cover_env["asset_id"]).image_id
            == "different-target-image"
        )
    if cover_env["route"].channel == "OFFICIAL_API":
        assert post_count(cover_env, gateway_wire) == (0 if already_present else 1)
    else:
        assert sum(
            c["method"] == "tools/call"
            and c["params"]["name"] == "creative_asset_share_get"
            for c in gateway_wire["wire"].calls
        ) == (0 if already_present else 1)
    if cover_env["route"].channel == "OFFICIAL_MCP":
        requests = [
            c["params"]
            for c in gateway_wire["wire"].calls
            if c["method"] == "tools/call"
        ]
        assert not [c for c in requests if c["name"] == "file_image_ad_upload"]
        if not already_present:
            request = next(
                c["arguments"]
                for c in requests
                if c["name"] == "creative_asset_share_get"
            )
            assert request["asset_type"] == "IMAGE" and request["material_ids"] == [
                "900001"
            ]
    else:
        posts = [call for call in gateway_wire["sdk_calls"] if call[0] == "POST"]
        assert all("/creative/asset/share/" in call[1] for call in posts)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("problem", ["mid", "signature", "revoked"])
def test_bad_source_prevents_image_share_and_never_uploads_target(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client, problem
):
    identity = build_queue(cover_env, database_engine).task_id
    data = image_data("source-cover.jpg", image_id="shared-image-id")
    if problem == "mid":
        data["list"][0].pop("material_id")
    elif problem == "signature":
        data["list"][0]["signature"] = "e" * 32
    else:
        with Session(database_engine) as db, db.begin():
            source = db.get(MaterialCoverJob, shared_cover)
            grant = db.get(
                BCAccountAccess,
                (
                    source.tenant_id,
                    source.bc_id,
                    source.advertiser_id,
                    source.connection_id,
                ),
            )
            grant.authorized = False
    if problem != "revoked":
        prepare_replies(cover_env, gateway_wire, [("file_image_ad_info_get", data)])
    run(cover_env, database_engine, redis_client, identity)
    assert job(database_engine, identity).status == "BLOCKED"
    assert post_count(cover_env, gateway_wire) == 0
