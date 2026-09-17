"""Real database/leases; cover sharing doubles only the official HTTP boundary."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import freeze_route
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverShareBatch
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.materials.test_covers import job_state, run, scopes, video_info
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_source_uploads import (
    MD5,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)
from tests.modules.materials.test_source_uploads import (
    wire as wire,
)


def enqueue(env, *, source=False, account="actual-account"):
    with Session(engine) as db, db.begin():
        method = (
            getattr(covers, "ensure_source_cover", None)
            if source
            else covers.ensure_cover
        )
        assert callable(method), (
            "source preparation needs an UPLOAD-only public entrypoint"
        )
        return method(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id=account,
            task_key=f"cover-test:{uuid4()}",
            route=freeze_route(
                db,
                context=env["context"],
                bc_id=env["bc_id"],
                connection_id=env["connection_id"],
            ),
        )


def test_source_cover_requires_upload_without_build_and_is_idempotent(source_env, wire):
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
        grant = db.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                "actual-account",
                source_env["connection_id"],
            ),
        )
        grant.can_build = False
    first = enqueue(source_env, source=True)
    second = enqueue(source_env, source=True)
    assert first.task_id == second.task_id
    assert job_state(first.task_id).purpose == "SOURCE"
    assert wire[0] == []


def test_source_image_mid_comes_from_readback_even_with_complete_upload_receipt(
    source_env, redis_client, wire
):
    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    identity = enqueue(source_env, source=True).task_id
    wire[1].extend(
        [
            video_info(),
            {
                "image_id": "tos-source",
                "signature": "a" * 32,
                "width": 720,
                "height": 1280,
                "displayable": False,
            },
        ]
    )
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(
        {
            "list": [
                {
                    "image_id": "tos-source",
                    "material_id": "900001",
                    "signature": "a" * 32,
                    "width": 720,
                    "height": 1280,
                    "displayable": False,
                }
            ]
        }
    )
    run(source_env, redis_client, identity, read=True)
    job = job_state(identity)
    assert (job.status, job.image_mid) == ("READY", "900001")


def page(rows):
    return {
        "list": rows,
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_number": len(rows),
            "total_page": 1,
        },
    }


def image(index, *, target_id=False):
    return {
        "image_id": f"tos-{'target' if target_id else 'source'}-{index}",
        "material_id": str((800000 if target_id else 900000) + index),
        "signature": f"{index + 1:032x}",
        "width": 720,
        "height": 1280,
        "displayable": False,
        "file_name": f"cover-{index}.jpg",
    }


def matrix(env, count=2, targets=2):
    scopes(env)
    with Session(engine) as db, db.begin():
        for n in range(targets):
            target(db, env, advertiser_id=f"target-{n}")
    ids = []
    for n in range(count):
        local = dict(env)
        with Session(engine) as db, db.begin():
            if n:
                material = MaterialFile(
                    tenant_id=env["context"].tenant_id,
                    bc_id=env["bc_id"],
                    file_name=f"{n}.mp4",
                    object_key=f"test/{uuid4()}",
                    byte_size=10,
                    sha256=f"{n:064x}",
                    video_md5=MD5,
                    storage_state="stored",
                )
                db.add(material)
                db.flush()
                local["material_id"] = material.id
            asset(db, local, "actual-account")
            for t in range(targets):
                asset(db, local, f"target-{t}")
        source_job_id = enqueue(local, source=True).task_id
        with Session(engine) as db, db.begin():
            job = db.get(MaterialCoverJob, source_job_id)
            job.status, job.request_armed_at = "READY", datetime.now(UTC)
            job.known_image_id, job.signature = (
                image(n)["image_id"],
                image(n)["signature"],
            )
            job.width, job.height, job.image_mid = 720, 1280, image(n)["material_id"]
            job.dispatch_id = None
            db.get(AccountMaterial, job.asset_id).image_id = job.known_image_id
        ids.extend(
            enqueue(local, account=f"target-{t}").task_id for t in range(targets)
        )
    return ids


def drive(env, redis_client, identity, *, read=False):
    for _ in range(1000):
        job = job_state(identity)
        if job.dispatch_id is None and job.error_code == "cover_source_pending":
            # 持久等待没有可消费消息；模拟下一轮正式恢复检查，不直接重跑空投递。
            with Session(engine) as db, db.begin():
                db.get(MaterialCoverJob, identity).repair_after = datetime.now(UTC)
                covers.repair_cover_dispatches(db)
            assert job_state(identity).dispatch_id is not None
        run(env, redis_client, identity, read=read)
        job = job_state(identity)
        if job.share_batch_id:
            with Session(engine) as db:
                batch = db.get(MaterialCoverShareBatch, job.share_batch_id)
                identity = batch.wake_job_id
                job = job_state(identity)
        if job.status not in ({"VERIFYING"} if read else {"PENDING", "PREPARING"}):
            return
    pytest.fail("bounded batch continuation did not finish")


def test_cover_planning_checks_only_one_twenty_by_ten_window(source_env, wire):
    """队列超过一组时不能扫描所有来源或按200个成员重复检查共同账户。"""
    from sqlalchemy import Engine, event

    from app.modules.materials.cover_sharing import _prepare

    identities = matrix(source_env, count=31, targets=10)
    first = job_state(identities[-1])
    source_queries, authorization_queries = [], []

    def observe(_connection, _cursor, statement, parameters, *_):
        if "FROM material_cover_job" in statement and "READY" in parameters.values():
            source_queries.append(statement)
        if "bc_connection_binding" in statement:
            authorization_queries.append(statement)

    event.listen(Engine, "before_cursor_execute", observe)
    try:
        with Session(engine) as db:
            transaction = db.begin()
            try:
                claimed = covers._claim_in_session(
                    db,
                    source_env["context"],
                    first.id,
                    first.dispatch_id,
                    first.revision,
                    read=False,
                )
                assert claimed is not None
                prepared = _prepare(db, source_env["context"], *claimed)
                assert prepared is not None
                batch, claims = prepared
                assert first.id in claims
                assert len(claims) == 200
                assert len({member["material_id"] for member in batch.members}) == 20
                assert len({member["advertiser_id"] for member in batch.members}) == 10
            finally:
                transaction.rollback()
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert wire[0] == []
    assert len(source_queries) <= 20, len(source_queries)
    assert len(authorization_queries) <= 150, len(authorization_queries)


@pytest.mark.parametrize("count,targets", [(2, 2), (20, 10)])
def test_missing_images_share_one_rectangle_and_publish_actual_target_ids(
    source_env, redis_client, wire, count, targets
):
    identities = matrix(source_env, count, targets)
    wire[1].extend(
        [
            {"list": [image(n) for n in range(count)]},
            *[page([]) for _ in range(targets)],
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    posts = [json.loads(call[2]["body"]) for call in wire[0] if call[0] == "POST"]
    assert posts == [
        {
            "advertiser_id": "actual-account",
            "asset_type": "IMAGE",
            "material_ids": [str(900000 + n) for n in range(count)],
            "shared_advertiser_ids": [f"target-{n}" for n in range(targets)],
        }
    ]
    wire[1].extend(
        page([image(n, target_id=True) for n in range(count)]) for _ in range(targets)
    )
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert all(
        job_state(identity).known_image_id.startswith("tos-target-")
        for identity in identities
    )


def test_unknown_share_only_reads_and_never_uploads_target(
    source_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    identities = matrix(source_env)
    wire[1].extend(
        [
            {"list": [image(0), image(1)]},
            page([]),
            page([]),
            ReadTimeoutError(None, "offline", "timeout"),
        ]
    )
    drive(source_env, redis_client, identities[0])
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    wire[1].extend([page([image(0, target_id=True), image(1, target_id=True)])] * 2)
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_build_without_source_never_uploads_target(source_env, redis_client, wire):
    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    identity = enqueue(source_env).task_id
    run(source_env, redis_client, identity)
    assert not wire[0]
    assert job_state(identity).status == "BLOCKED"


def test_source_event_adopts_unsent_build_identity_without_second_job(
    source_env, redis_client, wire
):
    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    original = enqueue(source_env).task_id
    run(source_env, redis_client, original)
    result = enqueue(source_env, source=True)
    assert result.task_id == original
    assert job_state(original).purpose == "SOURCE"
    assert result.state == "queued"
    assert wire[0] == []


def test_existing_source_image_gets_read_only_mid_job(source_env, redis_client, wire):
    scopes(source_env)
    with Session(engine) as db, db.begin():
        row = asset(db, source_env, "actual-account")
        row.image_id = "tos-existing"
    result = enqueue(source_env, source=True)
    assert result.task_id is not None
    assert job_state(result.task_id).request_armed_at is None
    wire[1].extend(
        [
            video_info(),
            {
                "list": [
                    {
                        "image_id": "tos-existing",
                        "material_id": "900005",
                        "signature": "a" * 32,
                        "file_name": job_state(result.task_id).remote_name,
                        "width": 720,
                        "height": 1280,
                        "displayable": False,
                    }
                ]
            },
            {
                "list": [
                    {
                        "image_id": "tos-source-inventory",
                        "material_id": "900005",
                        "signature": "a" * 32,
                        "file_name": "historical-image.jpg",
                        "width": 720,
                        "height": 1280,
                        "displayable": False,
                    }
                ],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            },
        ]
    )
    run(source_env, redis_client, result.task_id, read=True)
    assert job_state(result.task_id).status == "READY"
    assert job_state(result.task_id).image_mid == "900005"
    assert not [call for call in wire[0] if call[0] == "POST"]


def test_owned_build_candidate_auto_promotion_preserves_read_only_job(
    source_env, redis_client, wire
):
    from tests.modules.materials.test_source_uploads import seed_operation

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    seed_operation(
        source_env,
        status="succeeded",
        evidence={"video_id": "vid-actual-account", "mid": "900000"},
    )
    identity = enqueue(source_env).task_id
    with Session(engine) as db, db.begin():
        row = db.get(MaterialCoverJob, identity)
        row.candidate_image_id = "legacy-tos-id"
        row.signature, row.width, row.height = "a" * 32, 720, 1280
    run(source_env, redis_client, identity)
    assert job_state(identity).purpose == "SOURCE"
    assert job_state(identity).status == "VERIFYING"
    assert wire[0] == []
    info = {
        "image_id": "legacy-tos-id",
        "material_id": "900005",
        "signature": "a" * 32,
        "width": 720,
        "height": 1280,
        "displayable": False,
    }
    wire[1].extend(
        [
            {"list": [info]},
            page([{**info, "image_id": "account-inventory-tos-id"}]),
        ]
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert job_state(identity).request_armed_at is None
    assert not any(call[0] == "POST" for call in wire[0])


def test_build_schedules_missing_cover_on_recorded_video_upload_source(
    source_env, redis_client, wire
):
    from tests.modules.materials.test_source_uploads import seed_operation

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
        target(db, source_env, advertiser_id="target-account")
        asset(db, source_env, "target-account")
    seed_operation(
        source_env,
        status="succeeded",
        evidence={"video_id": "vid-actual-account", "mid": "900000"},
    )
    identity = enqueue(source_env, account="target-account").task_id
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    with Session(engine) as db:
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.advertiser_id == "actual-account",
                MaterialCoverJob.tenant_id == source_env["context"].tenant_id,
            )
        ).one()
        assert source.purpose == "SOURCE"
    assert wire[0] == []


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "native_share",
        "old_vid",
        "missing_owner",
        "wrong_connection",
        "conflicting_owner",
        "wrong_md5",
    ],
)
def test_ineligible_history_cannot_hide_owned_source_at_candidate_limit(
    source_env, redis_client, wire, invalid_kind
):
    from uuid import UUID

    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import TikTokConnection
    from app.modules.materials.models import MaterialAssetOperation

    scopes(source_env)
    base = uuid4().int & ~((1 << 32) - 1)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
        route = freeze_route(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            connection_id=source_env["connection_id"],
        )
        other_connection = TikTokConnection(tenant_id=source_env["context"].tenant_id)
        db.add(other_connection)
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=source_env["context"].tenant_id,
                bc_id=source_env["bc_id"],
                connection_id=other_connection.id,
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        for index in range(21):
            invalid = index < 20
            evidence = {"video_id": "vid-actual-account"}
            saved_route = route.model_dump(mode="json")
            path = "upload_original"
            if invalid:
                if invalid_kind == "old_vid":
                    evidence["video_id"] = "previous-vid"
                elif invalid_kind == "wrong_connection":
                    saved_route["connection_id"] = str(other_connection.id)
                else:
                    path = "share_source"
                    evidence.update(transport="url_relay", content_md5=MD5)
                    if invalid_kind == "native_share":
                        evidence["transport"] = "native_share"
                    elif invalid_kind == "conflicting_owner":
                        evidence.update(
                            upload_video_id="old-vid",
                            verified_upload_video_id="vid-actual-account",
                        )
                    elif invalid_kind == "wrong_md5":
                        evidence.update(
                            upload_video_id="vid-actual-account", content_md5="0" * 32
                        )
            db.add(
                MaterialAssetOperation(
                    id=UUID(int=base + index),
                    tenant_id=source_env["context"].tenant_id,
                    bc_id=source_env["bc_id"],
                    material_id=source_env["material_id"],
                    advertiser_id="actual-account",
                    path=path,
                    status="succeeded",
                    request_digest="a" * 64,
                    frozen_route=saved_route,
                    remote_response=evidence,
                )
            )
    identity = enqueue(source_env).task_id
    run(source_env, redis_client, identity)
    assert (job_state(identity).purpose, job_state(identity).status) == (
        "SOURCE",
        "PENDING",
    )
    assert wire[0] == []


def test_build_on_original_source_promotes_same_job_without_waiting_event(
    source_env, redis_client, wire
):
    from tests.modules.materials.test_source_uploads import seed_operation

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    seed_operation(
        source_env,
        status="succeeded",
        evidence={"video_id": "vid-actual-account", "mid": "900000"},
    )
    identity = enqueue(source_env).task_id
    run(source_env, redis_client, identity)
    assert (job_state(identity).purpose, job_state(identity).status) == (
        "SOURCE",
        "PENDING",
    )
    assert wire[0] == []
    wire[1].extend([video_info(), {"image_id": "tos-source"}])
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1
    assert not any("creative/asset/share" in call[1] for call in wire[0])


@pytest.mark.parametrize(
    "transport,receipt_vid,expected",
    [
        ("url_relay", "vid-actual-account", "SOURCE"),
        ("native_share", "vid-actual-account", "BUILD"),
        ("url_relay", "old-vid", "BUILD"),
    ],
)
def test_only_actual_url_relay_upload_proves_owned_source(
    source_env, redis_client, wire, transport, receipt_vid, expected
):
    from app.modules.materials.models import MaterialAssetOperation
    from tests.modules.materials.test_source_uploads import seed_operation

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    operation_id = seed_operation(
        source_env,
        status="succeeded",
        evidence={
            "video_id": "vid-actual-account",
            "upload_video_id": receipt_vid,
            "transport": transport,
            "content_md5": MD5,
        },
    )
    with Session(engine) as db, db.begin():
        db.get(MaterialAssetOperation, operation_id).path = "share_source"
    identity = enqueue(source_env).task_id
    run(source_env, redis_client, identity)
    assert job_state(identity).purpose == expected
    assert job_state(identity).status == (
        "PENDING" if expected == "SOURCE" else "BLOCKED"
    )
    assert wire[0] == []


@pytest.mark.parametrize(
    "upload_vid,verified_vid,expected",
    [
        (None, "vid-actual-account", "SOURCE"),
        ("old-vid", "vid-actual-account", "BUILD"),
        ("vid-actual-account", "old-vid", "BUILD"),
        (None, None, "BUILD"),
    ],
)
def test_reconciled_relay_owner_requires_consistent_readback_evidence(
    source_env, redis_client, wire, upload_vid, verified_vid, expected
):
    from app.modules.materials.models import MaterialAssetOperation
    from tests.modules.materials.test_source_uploads import seed_operation

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    operation_id = seed_operation(
        source_env,
        status="succeeded",
        evidence={
            "video_id": "vid-actual-account",
            "upload_video_id": upload_vid,
            "verified_upload_video_id": verified_vid,
            "transport": "url_relay",
            "content_md5": MD5,
        },
    )
    with Session(engine) as db, db.begin():
        db.get(MaterialAssetOperation, operation_id).path = "share_source"
    identity = enqueue(source_env).task_id
    run(source_env, redis_client, identity)
    assert job_state(identity).purpose == expected
    assert wire[0] == []


def test_image_inventory_scan_persists_progress_with_one_batch_wakeup(
    source_env, redis_client, wire
):
    from app.jobs.models import PendingDispatch

    identities = matrix(source_env, 2, 2)
    with Session(engine) as db:
        before = len(
            db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == source_env["context"].tenant_id
                )
            ).all()
        )
    wire[1].extend([{"list": [image(0), image(1)]}, page([])])
    run(source_env, redis_client, identities[0])
    assert job_state(identities[0]).status == "PENDING"
    assert len(wire[0]) == 2
    with Session(engine) as db:
        after = len(
            db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == source_env["context"].tenant_id
                )
            ).all()
        )
        assert after - before == 1
    wire[1].extend([page([]), {"failed_infos": {}}])
    run(source_env, redis_client, identities[0])
    assert job_state(identities[0]).status == "VERIFYING"
    assert len([call for call in wire[0] if "/file/image/ad/info/" in call[1]]) == 1


@pytest.mark.parametrize("eventually_complete", [True, False])
def test_overlapping_inventory_pages_require_complete_unique_census_before_share(
    source_env, redis_client, wire, eventually_complete
):
    """平台同总数分页会重叠；最多三轮补齐唯一库存，缺项时绝不发送。"""
    identity = matrix(source_env, 1, 1)[0]

    def inventory(indices, page_number):
        return {
            "list": [image(index, target_id=True) for index in indices],
            "page_info": {
                "page": page_number,
                "page_size": 100,
                "total_page": 2,
                "total_number": 102,
            },
        }

    wire[1].extend(
        [
            {"list": [image(0)]},
            inventory(range(1, 101), 1),
            inventory([100, 101], 2),
        ]
    )
    run(source_env, redis_client, identity)
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "PENDING"
    assert sum(call[0] == "POST" for call in wire[0]) == 0
    if eventually_complete:
        wire[1].extend(
            [
                inventory(range(1, 101), 1),
                inventory([101, 102], 2),
                {"failed_infos": {}},
            ]
        )
    else:
        for _ in range(2):
            wire[1].extend([inventory(range(1, 101), 1), inventory([100, 101], 2)])
    drive(source_env, redis_client, identity)
    current = job_state(identity)
    assert current.status == ("VERIFYING" if eventually_complete else "BLOCKED")
    assert sum(call[0] == "POST" for call in wire[0]) == int(eventually_complete)
    if not eventually_complete:
        assert current.error_code == "cover_search_incomplete"
        assert current.dispatch_id is None and current.request_armed_at is None


def test_target_waits_for_source_cover_without_polling_outbox(
    source_env, redis_client, wire
):
    from datetime import timedelta

    from app.jobs.models import PendingDispatch

    identity = matrix(source_env, 1, 1)[0]
    with Session(engine) as db, db.begin():
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.material_id == source_env["material_id"],
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        source.status = "VERIFYING"
        source_id = source.id
    run(source_env, redis_client, identity)
    current = job_state(identity)
    assert current.status == "PENDING" and current.dispatch_id is None
    assert current.error_code == "cover_source_pending"
    with Session(engine) as db:
        original = {
            row.id
            for row in db.exec(select(PendingDispatch)).all()
            if row.payload.get("job_id") == str(identity)
        }
    for _ in range(3):
        with Session(engine) as db, db.begin():
            db.get(MaterialCoverJob, identity).repair_after = datetime.now(
                UTC
            ) - timedelta(seconds=1)
            covers.repair_cover_dispatches(db)
    assert job_state(identity).dispatch_id is None
    with Session(engine) as db, db.begin():
        assert {
            row.id
            for row in db.exec(select(PendingDispatch)).all()
            if row.payload.get("job_id") == str(identity)
        } == original
        db.get(MaterialCoverJob, source_id).status = "READY"
        db.get(MaterialCoverJob, identity).repair_after = datetime.now(UTC) - timedelta(
            seconds=1
        )
        covers.repair_cover_dispatches(db)
    assert job_state(identity).dispatch_id is not None
    assert wire[0] == []


def test_retry_after_inventory_changed_starts_new_census_before_share(
    source_env, redis_client, wire
):
    identity = matrix(source_env, 1, 1)[0]

    def inventory(indices, page_number, total):
        return {
            "list": [image(index, target_id=True) for index in indices],
            "page_info": {
                "page": page_number,
                "page_size": 100,
                "total_page": 2,
                "total_number": total,
            },
        }

    wire[1].extend(
        [
            {"list": [image(0)]},
            inventory(range(1, 101), 1, 102),
            inventory([101, 102, 103], 2, 103),
        ]
    )
    run(source_env, redis_client, identity)
    run(source_env, redis_client, identity)
    assert job_state(identity).status == "BLOCKED"
    assert not any(call[0] == "POST" for call in wire[0])
    with Session(engine) as db, db.begin():
        covers.request_cover_retry(db, context=source_env["context"], job_id=identity)
    wire[1].extend(
        [
            inventory(range(1, 101), 1, 103),
            inventory([101, 102, 103], 2, 103),
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identity)
    assert job_state(identity).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_source_mid_change_between_pages_stops_before_share(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 1, 2)
    wire[1].extend([{"list": [image(0)]}, page([])])
    run(source_env, redis_client, identities[0])
    with Session(engine) as db, db.begin():
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == source_env["context"].tenant_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        source.image_mid = "999999"
    wire[1].extend([page([]), {"failed_infos": {}}])
    run(source_env, redis_client, identities[0])
    assert job_state(identities[0]).status == "BLOCKED"
    assert not any(call[0] == "POST" for call in wire[0])


def test_armed_image_batch_identity_is_database_immutable(
    source_env, redis_client, wire
):
    from sqlalchemy.exc import IntegrityError

    identities = matrix(source_env, 1, 1)
    wire[1].extend([{"list": [image(0)]}, page([]), {"failed_infos": {}}])
    run(source_env, redis_client, identities[0])
    with pytest.raises(IntegrityError):
        with Session(engine) as db, db.begin():
            batch = db.get(
                MaterialCoverShareBatch, job_state(identities[0]).share_batch_id
            )
            assert batch.armed_at is not None
            batch.members = [{**batch.members[0], "source_mid": "999999"}]


def test_unknown_image_reconciliation_reads_new_inventory(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 1, 1)
    wire[1].extend([{"list": [image(0)]}, page([]), {"failed_infos": {}}, page([])])
    drive(source_env, redis_client, identities[0])
    drive(source_env, redis_client, identities[0], read=True)
    assert job_state(identities[0]).status == "UNKNOWN"
    with Session(engine) as db, db.begin():
        covers.request_cover_reconciliation(
            db, context=source_env["context"], job_id=identities[0]
        )
    wire[1].append(page([image(0, target_id=True)]))
    drive(source_env, redis_client, identities[0], read=True)
    assert job_state(identities[0]).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_found_target_publishes_without_waiting_other_inventory(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 1, 2)
    wire[1].extend([{"list": [image(0)]}, page([image(0, target_id=True)])])
    run(source_env, redis_client, identities[0])
    assert job_state(identities[0]).status == "READY"
    assert job_state(identities[1]).status == "PENDING"
    wire[1].extend([page([]), {"failed_infos": {}}])
    run(source_env, redis_client, identities[1])
    assert job_state(identities[1]).status == "VERIFYING"


def test_member_repair_preserves_one_batch_wakeup(source_env, redis_client, wire):
    from datetime import timedelta

    identities = matrix(source_env, 2, 2)
    wire[1].extend([{"list": [image(0), image(1)]}, page([])])
    run(source_env, redis_client, identities[0])
    with Session(engine) as db, db.begin():
        for identity in identities:
            db.get(MaterialCoverJob, identity).repair_after = covers._now() - timedelta(
                seconds=90
            )
    with Session(engine) as db, db.begin():
        assert covers.repair_cover_dispatches(db) == 1
    with Session(engine) as db:
        assert (
            sum(
                db.get(MaterialCoverJob, identity).dispatch_id is not None
                for identity in identities
            )
            == 1
        )


def test_two_materials_with_same_source_image_keep_both_target_jobs(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 2, 2)
    with Session(engine) as db, db.begin():
        rows = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == source_env["context"].tenant_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).all()
        for job in rows:
            job.known_image_id = image(0)["image_id"]
            job.signature = image(0)["signature"]
            job.image_mid = image(0)["material_id"]
            db.get(AccountMaterial, job.asset_id).image_id = job.known_image_id
    wire[1].extend([{"list": [image(0)]}, page([]), page([]), {"failed_infos": {}}])
    drive(source_env, redis_client, identities[0])
    wire[1].extend([page([image(0, target_id=True)])] * 2)
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_existing_member_stale_read_remains_in_shared_batch(
    source_env, redis_client, wire
):
    from datetime import timedelta

    identities = matrix(source_env, 1, 2)
    wire[1].extend(
        [
            {"list": [image(0)]},
            page([image(0, target_id=True)]),
            page([]),
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    wire[1].append(page([image(0, target_id=True)]))
    drive(source_env, redis_client, identities[1], read=True)
    with Session(engine) as db, db.begin():
        row = db.get(MaterialCoverJob, identities[0])
        row.updated_at = covers._now() - timedelta(hours=2)
        covers.request_cover_reconciliation(
            db, context=source_env["context"], job_id=row.id
        )
    wire[1].append(page([image(0, target_id=True)]))
    drive(source_env, redis_client, identities[0], read=True)
    assert job_state(identities[0]).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_partial_publish_never_commits_without_recoverable_wake(
    source_env, redis_client, wire
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session as SASession

    identities = matrix(source_env, 1, 2)
    invalid_commits = []

    def check_committed(_session):
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    MaterialCoverJob.id,
                    MaterialCoverJob.status,
                    MaterialCoverJob.dispatch_id,
                    MaterialCoverJob.share_batch_id,
                ).where(MaterialCoverJob.id.in_(identities))
            ).all()
            if len(rows) == 2 and {row.status for row in rows} == {
                "READY",
                "PREPARING",
            }:
                waiting = next(row for row in rows if row.status == "PREPARING")
                wake = connection.execute(
                    select(MaterialCoverShareBatch.wake_job_id).where(
                        MaterialCoverShareBatch.id == waiting.share_batch_id
                    )
                ).scalar_one()
                if waiting.dispatch_id is None and waiting.id != wake:
                    invalid_commits.append(waiting.id)

    wire[1].extend([{"list": [image(0)]}, page([image(0, target_id=True)])])
    event.listen(SASession, "after_commit", check_committed)
    try:
        run(source_env, redis_client, identities[0])
    finally:
        event.remove(SASession, "after_commit", check_committed)
    assert not invalid_commits


def test_invalid_armed_search_stops_instead_of_endless_automatic_reads(
    source_env, redis_client, wire
):
    identities = matrix(source_env, 1, 1)
    wire[1].extend([{"list": [image(0)]}, page([]), {"failed_infos": {}}])
    drive(source_env, redis_client, identities[0])
    wire[1].append({"list": [], "page_info": {"page": "invalid"}})
    run(source_env, redis_client, identities[0], read=True)
    current = job_state(identities[0])
    assert current.status == "UNKNOWN" and current.dispatch_id is None
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_prepare_does_not_hold_anchor_while_waiting_for_material_lock(source_env):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    from app.modules.materials.cover_sharing import _prepare

    identities = matrix(source_env, 1, 1)
    job = job_state(identities[0])
    first, nonce = covers._claim(
        engine, source_env["context"], job.id, job.dispatch_id, job.revision, read=False
    )
    file_locked, lock_anchor, preparing = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    pid = []

    def ensure_lock_order():
        with Session(engine) as db, db.begin():
            db.exec(
                select(MaterialFile)
                .where(MaterialFile.id == first.material_id)
                .with_for_update()
            ).one()
            file_locked.set()
            assert lock_anchor.wait(10)
            db.exec(
                select(MaterialCoverJob)
                .where(MaterialCoverJob.id == first.id)
                .with_for_update()
            ).one()

    def prepare():
        assert file_locked.wait(10)
        with Session(engine) as db, db.begin():
            pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            preparing.set()
            return _prepare(db, source_env["context"], first, nonce)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(ensure_lock_order)
        worker = pool.submit(prepare)
        try:
            assert preparing.wait(10)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                with Session(engine) as db:
                    blocked = bool(
                        db.execute(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                            {"pid": pid[0]},
                        ).scalar_one()
                    )
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "prepare must reach the real PostgreSQL material lock"
        finally:
            lock_anchor.set()
        owner.result(timeout=10)
        assert worker.result(timeout=10) is not None


def test_source_and_build_race_keep_one_permanent_cover_identity(source_env, wire):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    scopes(source_env)
    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
    barrier = threading.Barrier(2)

    def start(source):
        barrier.wait(timeout=5)
        return enqueue(source_env, source=source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        source = pool.submit(start, True)
        build = pool.submit(start, False)
        first, second = source.result(timeout=10), build.result(timeout=10)
    assert first.task_id == second.task_id
    assert job_state(first.task_id).purpose == "SOURCE"
    assert wire[0] == []


def test_dead_armed_batch_repairs_one_read_and_never_reposts(
    source_env, redis_client, wire
):
    from datetime import timedelta

    identities = matrix(source_env, 2, 2)
    original = job_state(identities[0])
    wire[1].extend(
        [{"list": [image(0), image(1)]}, page([]), page([]), {"failed_infos": {}}]
    )
    drive(source_env, redis_client, identities[0])
    with Session(engine) as db, db.begin():
        for identity in identities:
            row = db.get(MaterialCoverJob, identity)
            if identity == original.id:
                covers._queue(db, row, read=False)
            row.status = "PREPARING"
            row.claim_token, row.claimed_until = (
                uuid4(),
                covers._now() - timedelta(seconds=90),
            )
            row.repair_after = row.claimed_until
            if identity != original.id:
                row.dispatch_id = None
    with Session(engine) as db, db.begin():
        assert covers.repair_cover_dispatches(db) == 1
    wire[1].extend([page([image(0, target_id=True), image(1, target_id=True)])] * 2)
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_slow_ten_target_scan_finishes_across_bounded_tasks(
    source_env, redis_client, wire
):
    import time

    identities = matrix(source_env, 1, 10)

    def delayed(value):
        def response():
            time.sleep(4)
            return value

        return response

    wire[1].extend(
        delayed(value)
        for value in [
            {"list": [image(0)]},
            *[page([]) for _ in range(10)],
            {"failed_infos": {}},
        ]
    )
    durations = []
    for _ in range(10):
        started = time.monotonic()
        run(source_env, redis_client, identities[0])
        durations.append(time.monotonic() - started)
    assert sum(durations) > covers.HARD_LIMIT
    assert max(durations) < covers.SOFT_LIMIT
    assert job_state(identities[0]).status == "VERIFYING"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_existing_target_image_skips_sharing(source_env, redis_client, wire):
    identities = matrix(source_env, 2, 2)
    wire[1].extend(
        [
            {"list": [image(0), image(1)]},
            page([image(0, target_id=True), image(1, target_id=True)]),
            page([image(0, target_id=True), image(1, target_id=True)]),
        ]
    )
    drive(source_env, redis_client, identities[0])
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert not [call for call in wire[0] if call[0] == "POST"]


def test_partial_share_failure_is_persisted_and_only_matching_images_publish(
    source_env, redis_client, wire
):
    from app.modules.materials.cover_models import MaterialCoverShareBatch

    identities = matrix(source_env, 1, 2)
    wire[1].extend(
        [
            {"list": [image(0)]},
            page([]),
            page([]),
            {"failed_infos": {"target-1": ["900000"]}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    with Session(engine) as db:
        batch = db.get(MaterialCoverShareBatch, job_state(identities[0]).share_batch_id)
        assert batch.failed_infos == {"target-1": ["900000"]}
    wire[1].extend(
        [
            page([image(0, target_id=True)]),
            page([{**image(0, target_id=True), "signature": "f" * 32}]),
        ]
    )
    drive(source_env, redis_client, identities[0], read=True)
    assert job_state(identities[0]).status == "READY"
    assert (job_state(identities[1]).status, job_state(identities[1]).error_code) == (
        "BLOCKED",
        "cover_share_rejected",
    )
    assert len([call for call in wire[0] if call[0] == "POST"]) == 1
