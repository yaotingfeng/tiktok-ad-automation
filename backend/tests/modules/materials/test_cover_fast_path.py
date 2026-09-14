"""完整封面回执与单任务会话：真实数据库/Redis，替身仅在官方 HTTP 边界。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
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
def test_target_verified_shared_cover_reuses_image_without_upload_or_fake_arm(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client
):
    assert shared_cover
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            (
                "file_image_ad_info_get",
                image_data("source-cover.jpg", image_id="shared-image-id"),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert current.candidate_image_id == "shared-image-id"
    assert current.known_image_id is None and current.request_armed_at is None
    with Session(database_engine) as db:
        assert (
            db.get(AccountMaterial, cover_env["asset_id"]).image_id == "shared-image-id"
        )
    if cover_env["route"].channel == "OFFICIAL_MCP":
        calls = gateway_wire["wire"].calls
        assert [c["params"]["name"] for c in calls if c["method"] == "tools/call"] == [
            "file_image_ad_info_get"
        ]
        assert sum(c["method"] == "initialize" for c in calls) == 1
    else:
        assert [c[0] for c in gateway_wire["sdk_calls"]] == ["GET"]


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_yesterdays_source_is_only_hint_and_expired_target_rechecks_same_id(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client
):
    with Session(database_engine) as db, db.begin():
        source = db.get(MaterialCoverJob, shared_cover)
        source.updated_at = datetime.now(UTC) - timedelta(days=1)
        db.get(AccountMaterial, source.asset_id).verified_at = source.updated_at
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            (
                "file_image_ad_info_get",
                image_data("old-name", image_id="shared-image-id"),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    assert job(database_engine, identity).status == "READY"
    assert queue(cover_env, database_engine).state == "ready"
    with Session(database_engine) as db, db.begin():
        db.get(MaterialCoverJob, identity).updated_at = datetime.now(UTC) - timedelta(
            days=1
        )
    assert queue(cover_env, database_engine).state == "queued"
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            (
                "file_image_ad_info_get",
                image_data("renamed", image_id="shared-image-id", displayable=False),
            ),
        ],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert covers.verified_cover_image_id(current) == "shared-image-id"
    assert current.known_image_id is None and current.request_armed_at is None
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_only_explicit_missing_reuse_candidate_falls_back_to_normal_upload(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client
):
    assert shared_cover
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            ("file_image_ad_info_get", {"list": []}),
            ("file_video_ad_info_get", video_data()),
            ("file_image_ad_upload", complete_receipt(cover_env)),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "READY"
    assert current.known_image_id == "actual-image-id" and current.request_armed_at
    assert current.candidate_image_id is None
    assert post_count(cover_env, gateway_wire) == 1
    if cover_env["route"].channel == "OFFICIAL_MCP":
        assert sum(c["method"] == "initialize" for c in gateway_wire["wire"].calls) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "problem", ["signature", "dimensions", "advertiser", "malformed"]
)
def test_bad_reuse_evidence_preserves_candidate_and_retry_is_read_only(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client, problem
):
    assert shared_cover
    changes = {"image_id": "shared-image-id"}
    changes.update(
        {
            "signature": {"signature": "d" * 32},
            "dimensions": {"width": 640, "height": 360},
            "advertiser": {"advertiser_id": "wrong-account"},
            "malformed": {"displayable": "false"},
        }[problem]
    )
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_image_ad_info_get", image_data("source", **changes))],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status in {"UNKNOWN", "BLOCKED"}
    assert current.candidate_image_id == "shared-image-id"
    assert covers.verified_cover_image_id(current) is None
    assert current.known_image_id is None and current.request_armed_at is None
    with Session(database_engine) as db, db.begin():
        covers.request_cover_reconciliation(
            db, context=cover_env["context"], job_id=identity
        )
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_image_ad_info_get", image_data("source", image_id="shared-image-id"))],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "READY"
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "problem", ["source_vid", "source_permission", "source_digest", "target_stale"]
)
def test_unusable_local_source_never_bypasses_target_video_verification(
    cover_env, shared_cover, gateway_wire, database_engine, redis_client, problem
):
    with Session(database_engine) as db, db.begin():
        source = db.get(MaterialCoverJob, shared_cover)
        if problem == "source_vid":
            db.get(AccountMaterial, source.asset_id).video_id = "changed-source-vid"
        elif problem == "source_digest":
            # The historical job digest is immutable. New material evidence
            # differs, so the target must verify its current VID independently.
            db.get(MaterialFile, cover_env["material_id"]).video_md5 = "d" * 32
        elif problem == "target_stale":
            db.get(AccountMaterial, cover_env["asset_id"]).verified_at = datetime.now(
                UTC
            ) - timedelta(days=1)
        else:
            db.exec(
                select(BCAccountAccess).where(
                    BCAccountAccess.tenant_id == source.tenant_id,
                    BCAccountAccess.advertiser_id == source.advertiser_id,
                )
            ).one().authorized = False
    identity = queue(cover_env, database_engine).task_id
    prepare_replies(
        cover_env,
        gateway_wire,
        [
            (
                "file_video_ad_info_get",
                video_data(signature=("d" if problem == "source_digest" else "a") * 32),
            ),
            ("file_image_ad_upload", complete_receipt(cover_env)),
        ],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status == "READY" and current.candidate_image_id is None
    assert current.known_image_id == "actual-image-id"
    assert post_count(cover_env, gateway_wire) == 1


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


def after_image_http(monkeypatch, channel, mutate):
    """Mutate only after the real official client received the image HTTP reply."""
    if channel == "OFFICIAL_API":
        import urllib3

        original = urllib3.PoolManager.request

        def response(pool, method, url, **kwargs):
            result = original(pool, method, url, **kwargs)
            if "/file/image/ad/info/" in url:
                mutate()
            return result

        monkeypatch.setattr(urllib3.PoolManager, "request", response)
    else:
        import json

        from app.integrations.tiktok.mcp import transport

        original = transport._new_http_transport

        class ResponseBoundary(original):
            async def handle_async_request(self, request):
                message = (
                    json.loads(request.content) if request.method == "POST" else {}
                )
                result = await super().handle_async_request(request)
                if (
                    message.get("method") == "tools/call"
                    and message["params"]["name"] == "file_image_ad_info_get"
                ):
                    mutate()
                return result

        monkeypatch.setattr(transport, "_new_http_transport", ResponseBoundary)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("exhaust", [False, True])
def test_unsent_network_failures_retry_same_cover_with_bounded_backoff(
    cover_env,
    shared_cover,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    candidate,
    exhaust,
):
    import json

    import httpx2

    from app.integrations.tiktok.mcp import transport

    identity = queue(cover_env, database_engine).task_id
    with Session(database_engine) as db, db.begin():
        if candidate:
            source = db.get(MaterialCoverJob, shared_cover)
            target = db.get(MaterialCoverJob, identity)
            target.candidate_image_id, target.signature = (
                source.known_image_id,
                source.signature,
            )
            target.width, target.height = source.width, source.height
            covers._queue(db, target, read=True)
        else:
            db.get(MaterialCoverJob, shared_cover).status = "BLOCKED"
    original = job(database_engine, identity)
    original_transport = transport._new_http_transport
    failures = []

    class OfflineHandshake(original_transport):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            if message.get("method") == "initialize" and len(failures) < (
                3 if exhaust else 2
            ):
                failures.append(True)
                raise httpx2.ConnectError("synthetic offline handshake")
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", OfflineHandshake)
    for attempt in range(1, (3 if exhaust else 2) + 1):
        started = datetime.now(UTC)
        run(cover_env, database_engine, redis_client, identity, read=candidate)
        current = job(database_engine, identity)
        assert current.failure_count == attempt
        assert current.request_armed_at is None and current.known_image_id is None
        assert (
            current.video_md5 == original.video_md5
            and current.frozen_route == original.frozen_route
        )
        assert current.candidate_image_id == original.candidate_image_id
        assert post_count(cover_env, gateway_wire) == 0
        assert not [
            c for c in gateway_wire["wire"].calls if c["method"] == "tools/call"
        ]
        if attempt < 3:
            assert current.status == ("VERIFYING" if candidate else "PENDING")
            with Session(database_engine) as db:
                dispatch = db.get(PendingDispatch, current.dispatch_id)
                assert dispatch.task_name == (
                    "materials.verify_cover" if candidate else "materials.prepare_cover"
                )
                assert dispatch.available_at >= started + timedelta(
                    seconds=5 * 2 ** (attempt - 1)
                )
        else:
            assert current.status == "BLOCKED" and current.dispatch_id is None
    if not exhaust:
        steps = (
            [
                (
                    "file_image_ad_info_get",
                    image_data("source", image_id="shared-image-id"),
                )
            ]
            if candidate
            else [
                ("file_video_ad_info_get", video_data()),
                ("file_image_ad_upload", complete_receipt(cover_env)),
            ]
        )
        prepare_replies(cover_env, gateway_wire, steps)
        run(cover_env, database_engine, redis_client, identity, read=candidate)
        current = job(database_engine, identity)
        assert current.status == "READY"
        assert current.failure_count == 0
        assert (
            current.video_md5 == original.video_md5
            and current.frozen_route == original.frozen_route
        )
        assert post_count(cover_env, gateway_wire) == (0 if candidate else 1)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_pinned_candidate_survives_twenty_newer_unrelated_ready_covers(
    cover_env, database_engine, shared_cover, gateway_wire, redis_client
):
    identity = queue(cover_env, database_engine).task_id
    with Session(database_engine) as db, db.begin():
        source = db.get(MaterialCoverJob, shared_cover)
        source.updated_at = datetime.now(UTC) - timedelta(minutes=1)
        target = db.get(MaterialCoverJob, identity)
        target.candidate_image_id, target.signature = (
            source.known_image_id,
            source.signature,
        )
        target.width, target.height = source.width, source.height
        for index in range(20):
            account = f"newer-cover-account-{index}"
            db.add(
                AdvertiserAccount(
                    tenant_id=source.tenant_id,
                    advertiser_id=account,
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                )
            )
            db.flush()
            db.add(
                BCAccountAccess(
                    tenant_id=source.tenant_id,
                    bc_id=source.bc_id,
                    advertiser_id=account,
                    connection_id=source.connection_id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    can_upload=True,
                    can_build=True,
                    permission_state="VERIFIED",
                    checked_at=datetime.now(UTC),
                )
            )
            db.flush()
            mapping = AccountMaterial(
                tenant_id=source.tenant_id,
                bc_id=source.bc_id,
                material_id=source.material_id,
                advertiser_id=account,
                connection_id=source.connection_id,
                video_id=f"newer-vid-{index}",
                image_id=f"other-image-{index}",
                status="available",
                verified_at=datetime.now(UTC),
            )
            db.add(mapping)
            db.flush()
            row = source.model_dump()
            row.update(
                id=uuid4(),
                asset_id=mapping.id,
                advertiser_id=account,
                video_id=mapping.video_id,
                known_image_id=mapping.image_id,
                updated_at=datetime.now(UTC),
            )
            db.add(MaterialCoverJob(**row))
        db.flush()
        assert covers._reuse_source(db, cover_env["context"], target).id == source.id
        covers._queue(db, target, read=True)
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_image_ad_info_get", image_data("source", image_id="shared-image-id"))],
    )
    run(cover_env, database_engine, redis_client, identity, read=True)
    assert job(database_engine, identity).status == "READY"
    assert post_count(cover_env, gateway_wire) == 0


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


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_MCP"], indirect=True)
@pytest.mark.parametrize("exhaust", [False, True])
def test_initial_candidate_read_unknown_retries_only_read_and_resets_after_ready(
    cover_env,
    shared_cover,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    exhaust,
):
    import json

    import httpx2

    from app.integrations.tiktok.mcp import transport

    assert shared_cover
    identity = queue(cover_env, database_engine).task_id
    original = job(database_engine, identity)
    original_transport = transport._new_http_transport
    failures = {"remaining": 3 if exhaust else 1}

    class OfflineCandidateRead(original_transport):
        async def handle_async_request(self, request):
            message = json.loads(request.content) if request.method == "POST" else {}
            if (
                message.get("method") == "tools/call"
                and message["params"]["name"] == "file_image_ad_info_get"
                and failures["remaining"]
            ):
                failures["remaining"] -= 1
                raise httpx2.ReadError("synthetic candidate read interruption")
            return await super().handle_async_request(request)

    monkeypatch.setattr(transport, "_new_http_transport", OfflineCandidateRead)
    for attempt in range(1, (3 if exhaust else 1) + 1):
        run(cover_env, database_engine, redis_client, identity, read=attempt > 1)
        current = job(database_engine, identity)
        assert current.status == ("BLOCKED" if attempt == 3 else "VERIFYING")
        assert current.failure_count == attempt
        assert current.candidate_image_id == "shared-image-id"
        assert current.known_image_id is None and current.request_armed_at is None
        assert (
            current.video_md5 == original.video_md5
            and current.frozen_route == original.frozen_route
        )
        assert post_count(cover_env, gateway_wire) == 0
        if attempt < 3:
            with Session(database_engine) as db:
                assert (
                    db.get(PendingDispatch, current.dispatch_id).task_name
                    == "materials.verify_cover"
                )
    if not exhaust:
        for refresh in (False, True):
            if refresh:
                with Session(database_engine) as db, db.begin():
                    db.get(MaterialCoverJob, identity).updated_at = datetime.now(
                        UTC
                    ) - timedelta(days=1)
                assert queue(cover_env, database_engine).state == "queued"
                failures["remaining"] = 1
                run(cover_env, database_engine, redis_client, identity, read=True)
                current = job(database_engine, identity)
                assert current.status == "VERIFYING" and current.failure_count == 1
            prepare_replies(
                cover_env,
                gateway_wire,
                [
                    (
                        "file_image_ad_info_get",
                        image_data("source", image_id="shared-image-id"),
                    )
                ],
            )
            run(cover_env, database_engine, redis_client, identity, read=True)
            current = job(database_engine, identity)
            assert current.status == "READY" and current.failure_count == 0
            assert current.known_image_id is None and current.request_armed_at is None
            assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "problem",
    [
        "claim",
        "source_permission",
        "source_vid",
        "target_vid",
        "target_stale",
        "digest",
    ],
)
def test_reuse_publication_rechecks_source_target_and_owner_after_actual_read(
    cover_env,
    shared_cover,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    problem,
):
    identity = queue(cover_env, database_engine).task_id
    replacement = uuid4()

    def mutate():
        with Session(database_engine) as db, db.begin():
            source = db.get(MaterialCoverJob, shared_cover)
            if problem == "claim":
                db.get(MaterialCoverJob, identity).claim_token = replacement
            elif problem == "source_vid":
                db.get(AccountMaterial, source.asset_id).video_id = "new-source-vid"
            elif problem == "target_vid":
                db.get(
                    AccountMaterial, cover_env["asset_id"]
                ).video_id = "new-target-vid"
            elif problem == "target_stale":
                db.get(AccountMaterial, cover_env["asset_id"]).verified_at = (
                    datetime.now(UTC) - timedelta(days=1)
                )
            elif problem == "digest":
                db.get(MaterialFile, cover_env["material_id"]).video_md5 = "d" * 32
            else:
                db.exec(
                    select(BCAccountAccess).where(
                        BCAccountAccess.tenant_id == source.tenant_id,
                        BCAccountAccess.advertiser_id == source.advertiser_id,
                    )
                ).one().authorized = False

    after_image_http(monkeypatch, cover_env["route"].channel, mutate)
    prepare_replies(
        cover_env,
        gateway_wire,
        [("file_image_ad_info_get", image_data("source", image_id="shared-image-id"))],
    )
    run(cover_env, database_engine, redis_client, identity)
    current = job(database_engine, identity)
    assert current.status != "READY" and current.candidate_image_id == "shared-image-id"
    assert current.known_image_id is None and current.request_armed_at is None
    if problem == "claim":
        assert current.claim_token == replacement
    with Session(database_engine) as db:
        assert db.get(AccountMaterial, cover_env["asset_id"]).image_id is None
    assert post_count(cover_env, gateway_wire) == 0
