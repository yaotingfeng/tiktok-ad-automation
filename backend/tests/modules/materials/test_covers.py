"""Cover jobs use real PostgreSQL/Redis and only fake official SDK transport."""

from uuid import uuid4

from sqlmodel import Session, select

from app.core.db import engine
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial
from tests.modules.materials.test_readiness import asset
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def queue(env):
    with Session(engine) as session, session.begin():
        return covers.ensure_cover(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            material_id=env["material_id"],
            advertiser_id="actual-account",
            task_key=f"build:{uuid4()}",
        )


def seed(env):
    with Session(engine) as session, session.begin():
        row = asset(session, env, "actual-account")
        return row.id


def test_shared_cover_job_is_local_and_reuses_actual_video_identity(source_env, wire):
    seed(source_env)
    first, second = queue(source_env), queue(source_env)
    assert first.state == second.state == "queued"
    assert first.task_id == second.task_id
    with Session(engine) as session:
        job = session.get(MaterialCoverJob, first.task_id)
        assert job.video_id == "vid-actual-account"
        assert job.connection_id == source_env["connection_id"]
        assert job.request_armed_at is None and job.known_image_id is None
        assert len(session.exec(select(MaterialCoverJob)).all()) == 1
    assert not wire[0]


def test_existing_image_is_ready_without_enqueue_or_source_copy(source_env, wire):
    identity = seed(source_env)
    with Session(engine) as session, session.begin():
        session.get(AccountMaterial, identity).image_id = "verified-target-image"
    result = queue(source_env)
    assert result.state == "ready"
    assert result.mapping.image_id == "verified-target-image"
    assert result.task_id is None and not wire[0]


def job_state(identity):
    with Session(engine) as session:
        value = session.get(MaterialCoverJob, identity)
        session.expunge(value)
        return value


def run(env, redis_client, identity, *, read=False, dispatch=None, revision=None):
    job = job_state(identity)
    covers.run_cover(
        database_engine=engine,
        redis_client=redis_client,
        context=env["context"],
        job_id=identity,
        dispatch_id=dispatch or job.dispatch_id,
        revision=revision or job.revision,
        read=read,
    )


def scopes(env):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import TikTokConnection

    with Session(engine) as session, session.begin():
        session.get(
            TikTokConnection, env["connection_id"]
        ).credential_ciphertext = encrypt_credentials(
            tenant_id=env["context"].tenant_id,
            value={"access_token": "offline-token-secret", "scope": "[6]"},
        )


def video_info(url="https://example.com/temporary-secret"):
    from tests.modules.materials.test_source_uploads import MD5

    return {
        "list": [
            {
                "video_id": "vid-actual-account",
                "displayable": True,
                "signature": MD5,
                "width": 720,
                "height": 1280,
                "video_cover_url": url,
            }
        ]
    }


def image_info(identity, **changes):
    return {
        "list": [
            {
                "image_id": "target-image",
                "file_name": job_state(identity).remote_name,
                "displayable": True,
                "signature": "a" * 32,
                "width": 360,
                "height": 640,
                **changes,
            }
        ]
    }


def successful_upload(env, redis_client, wire):
    scopes(env)
    seed(env)
    result = queue(env)
    wire[1].extend([video_info(), {"image_id": "target-image", "signature": "a" * 32}])
    run(env, redis_client, result.task_id)
    return result.task_id


def test_upload_arms_before_wire_and_readback_updates_only_target(
    source_env, redis_client, wire
):
    import json

    scopes(source_env)
    mapping_id = seed(source_env)
    identity = queue(source_env).task_id

    def upload():
        assert job_state(identity).request_armed_at is not None
        return {"image_id": "target-image", "signature": "a" * 32}

    wire[1].extend([video_info(), upload])
    old = job_state(identity)
    run(source_env, redis_client, identity)
    assert job_state(identity).known_image_id == "target-image"
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    with Session(engine) as session:
        mapping = session.get(AccountMaterial, mapping_id)
        assert (
            mapping.image_id == "target-image"
            and mapping.video_id == "vid-actual-account"
        )
        assert mapping.cover_url is None
    run(
        source_env,
        redis_client,
        identity,
        dispatch=old.dispatch_id,
        revision=old.revision,
    )
    assert [call[0] for call in wire[0]] == ["GET", "POST", "GET"]
    assert json.loads(wire[0][1][2]["body"])["upload_type"] == "UPLOAD_BY_URL"
    assert "temporary-secret" not in job_state(identity).model_dump_json()


def test_unknown_upload_empty_search_stops_until_explicit_read_only_reconcile(
    source_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id
    wire[1].extend([video_info(), ReadTimeoutError(None, "offline", "SECRET")])
    run(source_env, redis_client, identity)
    assert queue(source_env).reason_code == "cover_result_unknown"
    wire[1].append(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 0,
                "total_number": 0,
            },
        }
    )
    run(source_env, redis_client, identity, read=True)
    assert (
        job_state(identity).status == "UNKNOWN"
        and job_state(identity).dispatch_id is None
    )
    assert queue(source_env).state == "blocked"
    with Session(engine) as session, session.begin():
        assert covers.repair_cover_dispatches(session) == 0
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(
        {
            "list": [],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 0,
                "total_number": 0,
            },
        }
    )
    run(source_env, redis_client, identity, read=True)
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_expired_ready_cover_get_does_not_refresh_or_enqueue_but_ensure_queues_info(
    source_env, redis_client, wire
):
    from datetime import timedelta

    from app.core.config import settings

    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    with Session(engine) as session, session.begin():
        job = session.get(MaterialCoverJob, identity)
        job.updated_at -= timedelta(seconds=settings.MATERIAL_ASSET_MAX_AGE_SECONDS + 1)
    with Session(engine) as session, session.begin():
        result = covers.get_cover_status(
            session, context=source_env["context"], job_id=identity
        )
        assert result.state != "ready"
    assert job_state(identity).dispatch_id is None
    assert queue(source_env).state == "queued"
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_unarmed_permission_failure_can_explicitly_retry_same_identity(
    source_env, redis_client, wire
):
    seed(source_env)
    identity = queue(source_env).task_id
    run(source_env, redis_client, identity)
    old = job_state(identity)
    assert old.status == "BLOCKED" and old.request_armed_at is None
    scopes(source_env)
    with Session(engine) as session, session.begin():
        value = covers.request_cover_retry(
            session, context=source_env["context"], job_id=identity
        )
        assert value.task_id == identity and value.state == "queued"
    assert job_state(identity).remote_name == old.remote_name
    wire[1].extend([video_info(), {"image_id": "target-image", "signature": "a" * 32}])
    run(source_env, redis_client, identity)
    assert job_state(identity).known_image_id == "target-image"


def unknown(env, redis_client, wire):
    from urllib3.exceptions import ReadTimeoutError

    scopes(env)
    seed(env)
    identity = queue(env).task_id
    wire[1].extend([video_info(), ReadTimeoutError(None, "offline", "SECRET")])
    run(env, redis_client, identity)
    return identity


def search_page(rows, *, page=1, total=None):
    total = len(rows) if total is None else total
    return {
        "list": rows,
        "page_info": {
            "page": page,
            "page_size": 100,
            "total_page": (total + 99) // 100,
            "total_number": total,
        },
    }


def test_search_completes_every_page_before_unique_name_info(
    source_env, redis_client, wire
):
    from app.modules.materials.cover_models import MaterialCoverJobPage

    identity = unknown(source_env, redis_client, wire)
    first = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    first[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(first, total=101))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).next_page == 2
    assert job_state(identity).known_image_id is None
    wire[1].append(
        search_page([{"image_id": "last", "file_name": "other.jpg"}], page=2, total=101)
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).known_image_id == "target-image"
    assert job_state(identity).status == "VERIFYING"
    wire[1].append(image_info(identity))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    with Session(engine) as session:
        pages = session.exec(
            select(MaterialCoverJobPage).where(MaterialCoverJobPage.job_id == identity)
        ).all()
        assert sorted(len(page.image_ids) for page in pages) == [1, 100]
    assert [call[0] for call in wire[0]].count("POST") == 1


def test_search_ambiguity_after_first_page_match_never_marks_ready(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    rows[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(
        search_page(
            image_info(identity, image_id="second-matching-image")["list"],
            page=2,
            total=101,
        )
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).known_image_id is None
    assert job_state(identity).dispatch_id is None


def test_duplicate_cross_page_id_and_changed_total_stop_incomplete_scan(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(search_page([rows[0]], page=2, total=101))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).error_code == "cover_search_incomplete"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(search_page(rows, total=101))
    run(source_env, redis_client, identity, read=True)
    wire[1].append(
        search_page(
            [
                {"image_id": "new", "file_name": "new.jpg"},
                {"image_id": "new2", "file_name": "new2.jpg"},
            ],
            page=2,
            total=102,
        )
    )
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert job_state(identity).error_code == "cover_search_incomplete"


def test_search_at_official_10000_visibility_cap_is_incomplete(
    source_env, redis_client, wire
):
    identity = unknown(source_env, redis_client, wire)
    rows = [
        {"image_id": f"other-{i}", "file_name": "unrelated.jpg"} for i in range(100)
    ]
    rows[0] = image_info(identity)["list"][0]
    wire[1].append(search_page(rows, total=10000))
    run(source_env, redis_client, identity, read=True)
    assert (
        job_state(identity).status == "UNKNOWN"
        and job_state(identity).known_image_id is None
    )
    assert job_state(identity).error_code == "cover_search_incomplete"


def test_known_id_wrong_name_hash_or_geometry_never_ready(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)
    wire[1].append(image_info(identity, file_name="foreign.jpg"))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(image_info(identity, signature="b" * 32))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session, session.begin():
        covers.request_cover_reconciliation(
            session, context=source_env["context"], job_id=identity
        )
    wire[1].append(image_info(identity, width=640, height=360))
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    assert all("search" not in call[1] for call in wire[0])


def test_changed_video_during_image_read_does_not_attach_old_cover(
    source_env, redis_client, wire
):
    identity = successful_upload(source_env, redis_client, wire)

    def change_video():
        data = image_info(identity)
        with Session(engine) as session, session.begin():
            session.get(
                AccountMaterial, job_state(identity).asset_id
            ).video_id = "replaced-vid"
        return data

    wire[1].append(change_video)
    run(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "UNKNOWN"
    with Session(engine) as session:
        assert (
            session.get(AccountMaterial, job_state(identity).asset_id).image_id is None
        )


def test_revoke_between_video_get_and_post_blocks_without_arming(
    source_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess

    scopes(source_env)
    seed(source_env)
    identity = queue(source_env).task_id

    def revoke():
        with Session(engine) as session, session.begin():
            grant = session.get(
                BCAccountAccess,
                (
                    source_env["context"].tenant_id,
                    source_env["bc_id"],
                    "actual-account",
                    source_env["connection_id"],
                ),
            )
            grant.can_upload = False
        return video_info()

    wire[1].append(revoke)
    run(source_env, redis_client, identity)
    assert job_state(identity).request_armed_at is None
    assert job_state(identity).status == "BLOCKED"
    assert [call[0] for call in wire[0]] == ["GET"]
