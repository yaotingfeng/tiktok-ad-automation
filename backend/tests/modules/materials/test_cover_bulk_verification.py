"""真实任务、PG 和 Redis：已知封面核查应合并 HTTP，保持每项执行权。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session

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


def known_jobs(env, engine, count, *, stale=True):
    identities = []
    for index in range(count):
        if index:
            with Session(engine) as db, db.begin():
                material = MaterialFile(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["route"].bc_id,
                    file_name=f"bulk-{index}.mp4",
                    object_key=f"offline/{uuid4()}",
                    byte_size=120,
                    video_md5="a" * 32,
                    sha256="b" * 64,
                    storage_state="unavailable",
                )
                db.add(material)
                db.flush()
                asset = AccountMaterial(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["route"].bc_id,
                    material_id=material.id,
                    advertiser_id=env["advertiser"],
                    connection_id=env["route"].connection_id,
                    video_id=f"video-{index}",
                    status="available",
                    verified_at=datetime.now(UTC),
                )
                db.add(asset)
                db.flush()
                item_env = {**env, "material_id": material.id, "asset_id": asset.id}
        else:
            item_env = env
        identity = queue(item_env, engine).task_id
        with Session(engine) as db, db.begin():
            current = db.get(MaterialCoverJob, identity)
            current.request_armed_at = datetime.now(UTC)
            current.known_image_id = f"image-{index}"
            current.signature = "c" * 32
            current.width, current.height = 360, 640
            if stale:
                db.get(AccountMaterial, current.asset_id).verified_at = datetime.now(
                    UTC
                ) - timedelta(hours=1)
            covers._queue(db, current, read=True)
        identities.append(identity)
    return identities


def replies(env, wire, engine, identities, *, missing_image=None, missing_video=None):
    videos, images = [], []
    for identity in identities:
        current = job(engine, identity)
        if current.video_id != missing_video:
            videos.extend(video_data(video_id=current.video_id)["list"])
        if current.known_image_id != missing_image:
            images.extend(
                image_data(current.remote_name, image_id=current.known_image_id)["list"]
            )
    prepare_replies(
        env,
        wire,
        [
            ("file_video_ad_info_get", {"list": videos}),
            ("file_image_ad_info_get", {"list": images}),
        ],
    )


def physical_calls(env, wire):
    return (
        wire["sdk_calls"]
        if env["route"].channel == "OFFICIAL_API"
        else [c for c in wire["wire"].calls if c["method"] == "tools/call"]
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_fifty_known_covers_do_not_repeat_account_authorization_per_member(
    cover_env, gateway_wire, database_engine, redis_client
):
    """逐成员重复共同授权会耗尽线上45秒预算；内容/claim仍逐项检查。"""
    from sqlalchemy import Engine, event

    identities = known_jobs(cover_env, database_engine, 50)
    replies(cover_env, gateway_wire, database_engine, identities)
    authorization_queries = []

    def observed(_connection, _cursor, statement, _parameters, *_):
        if "bc_connection_binding" in statement:
            authorization_queries.append(statement)

    event.listen(Engine, "before_cursor_execute", observed)
    try:
        run(cover_env, database_engine, redis_client, identities[0], read=True)
    finally:
        event.remove(Engine, "before_cursor_execute", observed)
    assert all(
        job(database_engine, identity).status == "READY" for identity in identities
    )
    assert len(physical_calls(cover_env, gateway_wire)) == 2
    assert post_count(cover_env, gateway_wire) == 0
    # MCP 的协议握手/逐 HTTP 重新授权仍执行；不能把这些必要检查算作重复成员。
    assert len(authorization_queries) <= 120, len(authorization_queries)


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_cover_authorization_reuse_cannot_escape_its_transaction(
    cover_env, database_engine
):
    from app.core.errors import DomainError

    identities = known_jobs(cover_env, database_engine, 1)
    current = job(database_engine, identities[0])
    with Session(database_engine) as db:
        with db.begin():
            checks = covers._CoverAccessChecks(db, cover_env["context"])
            covers._access(db, cover_env["context"], current, checks=checks)
            checks.preload_admission([current])
            assert checks.admitted(db, cover_env["context"], current)
        with db.begin(), pytest.raises(DomainError, match="封面授权核查事务已变化"):
            covers._access(db, cover_env["context"], current, checks=checks)
        with db.begin(), pytest.raises(DomainError, match="封面授权核查事务已变化"):
            checks.admitted(db, cover_env["context"], current)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_23_known_covers_run_via_production_task_with_two_calls_and_duplicate_delivery_noops(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 23)
    delivered = [job(database_engine, identity) for identity in identities]
    replies(cover_env, gateway_wire, database_engine, identities)
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY"
    ] * 23
    assert len(physical_calls(cover_env, gateway_wire)) == 2
    assert post_count(cover_env, gateway_wire) == 0
    for original in delivered:
        covers.run_cover(
            database_engine=database_engine,
            redis_client=redis_client,
            context=cover_env["context"],
            job_id=original.id,
            dispatch_id=original.dispatch_id,
            revision=original.revision,
            read=True,
        )
    assert len(physical_calls(cover_env, gateway_wire)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_revoked_member_is_excluded_before_first_call_without_poisoning_peers(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 3)
    with Session(database_engine) as db, db.begin():
        current = db.get(MaterialCoverJob, identities[1])
        db.get(AccountMaterial, current.asset_id).video_id = "replaced"
    replies(cover_env, gateway_wire, database_engine, [identities[0], identities[2]])
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY",
        "UNKNOWN",
        "READY",
    ]
    assert job(database_engine, identities[1]).error_code == "cover_video_changed"
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize("count", [51, 100])
@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_fresh_covers_partition_at_50_and_never_drop_the_last_jobs(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    count,
):
    identities = known_jobs(cover_env, database_engine, count, stale=False)
    following = sorted(
        identity for identity in identities[1:] if identity > identities[0]
    )
    preceding = sorted(
        identity for identity in identities[1:] if identity < identities[0]
    )
    selected = [identities[0], *(following + preceding)[:49]]
    remaining = [identity for identity in identities if identity not in selected]
    steps = []
    for group in (selected, remaining):
        rows = []
        for identity in group:
            current = job(database_engine, identity)
            rows.extend(
                image_data(current.remote_name, image_id=current.known_image_id)["list"]
            )
        steps.append(("file_image_ad_info_get", {"list": rows}))
    prepare_replies(cover_env, gateway_wire, steps)
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert (
        sum(job(database_engine, identity).status == "READY" for identity in identities)
        == 50
    )
    run(cover_env, database_engine, redis_client, remaining[0], read=True)
    assert all(
        job(database_engine, identity).status == "READY" for identity in identities
    )
    assert len(physical_calls(cover_env, gateway_wire)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_image_only_blocks_its_job_and_cannot_upload(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 3)
    replies(
        cover_env, gateway_wire, database_engine, identities, missing_image="image-1"
    )
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY",
        "UNKNOWN",
        "READY",
    ]
    assert job(database_engine, identities[1]).known_image_id == "image-1"
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_unknown_known_receipts_reconcile_only_by_reads_without_reupload(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 3)
    for identity in identities:
        with Session(database_engine) as db, db.begin():
            current = db.get(MaterialCoverJob, identity)
            covers._stop(current, "cover_result_unknown", unknown=True)
            covers.request_cover_reconciliation(
                db, context=cover_env["context"], job_id=identity
            )
    replies(cover_env, gateway_wire, database_engine, identities)
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY"
    ] * 3
    assert [
        job(database_engine, identity).known_image_id for identity in identities
    ] == ["image-0", "image-1", "image-2"]
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_bulk_task_does_not_open_one_database_session_per_member(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    from sqlalchemy import Engine, event

    identities = known_jobs(cover_env, database_engine, 23)
    replies(cover_env, gateway_wire, database_engine, identities)
    connections = []

    def observed(_connection):
        connections.append(1)

    event.listen(Engine, "engine_connect", observed)
    try:
        run(cover_env, database_engine, redis_client, identities[0], read=True)
    finally:
        event.remove(Engine, "engine_connect", observed)
    assert all(
        job(database_engine, identity).status == "READY" for identity in identities
    )
    assert len(connections) < 30
    print(f"BULK_CONNECTIONS jobs=23 connections={len(connections)}")  # noqa: T201


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_last_expired_cover_refreshes_video_before_image_without_extending_validity(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 1)
    replies(cover_env, gateway_wire, database_engine, identities)
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert job(database_engine, identities[0]).status == "READY"
    assert len(physical_calls(cover_env, gateway_wire)) == 2


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_missing_video_stays_unknown_while_other_images_become_ready(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 3)
    replies(
        cover_env,
        gateway_wire,
        database_engine,
        identities,
        missing_video="video-1",
        missing_image="image-1",
    )
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY",
        "UNKNOWN",
        "READY",
    ]
    with Session(database_engine) as db:
        current = db.get(MaterialCoverJob, identities[1])
        assert db.get(AccountMaterial, current.asset_id).image_id is None
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_fresh_target_mappings_only_require_one_bulk_image_read(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
):
    identities = known_jobs(cover_env, database_engine, 23, stale=False)
    images = [
        image_data("existing", image_id=f"image-{index}")["list"][0]
        for index in range(23)
    ]
    prepare_replies(
        cover_env, gateway_wire, [("file_image_ad_info_get", {"list": images})]
    )
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert [job(database_engine, identity).status for identity in identities] == [
        "READY"
    ] * 23
    assert len(physical_calls(cover_env, gateway_wire)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "mutation", ["claim", "authority", "video", "job_identity", "mapped_identity"]
)
def test_changed_member_after_bulk_video_read_prevents_image_send(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    monkeypatch,
    mutation,
):
    from sqlmodel import select

    from app.modules.accounts.connection_models import ConnectionAuthorization
    from tests.modules.accounts.test_material_gateway import after_material_http

    identities = known_jobs(cover_env, database_engine, 3)
    changed = []
    replacement = uuid4()

    def mutate():
        if changed:
            return
        with Session(database_engine) as db, db.begin():
            current = db.get(MaterialCoverJob, identities[1])
            if mutation == "claim":
                current.claim_token = replacement
            elif mutation == "authority":
                facts = db.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id
                        == cover_env["route"].connection_id
                    )
                ).one()
                facts.permission_summary = {
                    **facts.permission_summary,
                    "read_authorized": False,
                }
            elif mutation == "job_identity":
                current.known_image_id = "replacement-image"
            elif mutation == "mapped_identity":
                current.video_id = "replacement-video"
                db.get(AccountMaterial, current.asset_id).video_id = "replacement-video"
            else:
                db.get(AccountMaterial, current.asset_id).video_id = "replacement-video"
        changed.append(True)

    after_material_http(monkeypatch, cover_env["route"].channel, mutate)
    replies(
        cover_env, gateway_wire, database_engine, identities, missing_image="image-1"
    )
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert changed
    assert len(physical_calls(cover_env, gateway_wire)) == (
        1 if mutation == "authority" else 2
    )
    if mutation == "authority":
        assert all(
            job(database_engine, identity).status != "READY" for identity in identities
        )
    else:
        assert job(database_engine, identities[0]).status == "READY"
        assert job(database_engine, identities[2]).status == "READY"
        assert job(database_engine, identities[1]).status != "READY"
        import json

        last_call = physical_calls(cover_env, gateway_wire)[-1]
        requested = (
            json.loads(dict(last_call[2]["fields"])["image_ids"])
            if cover_env["route"].channel == "OFFICIAL_API"
            else last_call["params"]["arguments"]["image_ids"]
        )
        assert set(requested) == {"image-0", "image-2"}
    if mutation == "claim":
        assert job(database_engine, identities[1]).claim_token == replacement
    if mutation == "mapped_identity":
        with Session(database_engine) as db:
            current = db.get(MaterialCoverJob, identities[1])
            assert db.get(AccountMaterial, current.asset_id).verified_at < datetime.now(
                UTC
            ) - timedelta(minutes=15)
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_authorization_revoked_after_image_response_prevents_cover_publication(
    cover_env, gateway_wire, database_engine, redis_client, monkeypatch
):
    from sqlmodel import select

    from app.modules.accounts.connection_models import ConnectionAuthorization
    from tests.modules.accounts.test_material_gateway import after_material_http

    identities = known_jobs(cover_env, database_engine, 3)
    calls = []

    def revoke_after_image():
        calls.append(True)
        if len(calls) != 2:
            return
        with Session(database_engine) as db, db.begin():
            authorization = db.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id
                    == cover_env["route"].connection_id
                )
            ).one()
            authorization.permission_summary = {
                **authorization.permission_summary,
                "read_authorized": False,
            }

    if cover_env["route"].channel == "OFFICIAL_API":
        import urllib3

        original = urllib3.PoolManager.request

        def response(pool, method, url, **kwargs):
            result = original(pool, method, url, **kwargs)
            if "/file/video/ad/info/" in url or "/file/image/ad/info/" in url:
                revoke_after_image()
            return result

        monkeypatch.setattr(urllib3.PoolManager, "request", response)
    else:
        after_material_http(monkeypatch, cover_env["route"].channel, revoke_after_image)
    replies(cover_env, gateway_wire, database_engine, identities)
    run(cover_env, database_engine, redis_client, identities[0], read=True)
    assert len(calls) == 2
    assert all(
        job(database_engine, identity).status != "READY" for identity in identities
    )
    with Session(database_engine) as db:
        assert all(
            db.get(AccountMaterial, job(database_engine, identity).asset_id).image_id
            is None
            for identity in identities
        )
    assert post_count(cover_env, gateway_wire) == 0


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("change", ["due", "phase"])
def test_candidate_changed_between_selection_and_claim_is_left_for_its_own_dispatch(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    change,
):
    from sqlalchemy import Engine, event

    from app.jobs.models import PendingDispatch

    identities = known_jobs(cover_env, database_engine, 3)
    changed = []

    def mutate(_conn, _cursor, statement, _params, *_):
        if changed or "UNION ALL" not in statement:
            return
        changed.append(True)
        with Session(database_engine) as db, db.begin():
            current = db.get(MaterialCoverJob, identities[1])
            if change == "due":
                db.get(PendingDispatch, current.dispatch_id).available_at = (
                    datetime.now(UTC) + timedelta(hours=1)
                )
            else:
                current.status = "PENDING"

    replies(cover_env, gateway_wire, database_engine, [identities[0], identities[2]])
    event.listen(Engine, "after_cursor_execute", mutate)
    try:
        run(cover_env, database_engine, redis_client, identities[0], read=True)
    finally:
        event.remove(Engine, "after_cursor_execute", mutate)
    assert changed
    assert job(database_engine, identities[1]).status == (
        "PENDING" if change == "phase" else "VERIFYING"
    )
    assert job(database_engine, identities[1]).claim_token is None
    first_result = job(database_engine, identities[0])
    assert first_result.status == "READY", first_result.error_code
    assert job(database_engine, identities[2]).status == "READY"
