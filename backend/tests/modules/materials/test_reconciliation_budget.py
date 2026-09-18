"""整轮目标只读核查有界；真实 PG/Redis，平台仅在传输边界替身。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select
from urllib3.exceptions import ReadTimeoutError

from app.core.config import settings
from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import (
    queue_distribution,
    repair_material_dispatches,
)
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_source_uploads import CONTENT, info
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def unknown(env, *, known=True, budget=None, account=None):
    if account is None:
        with Session(engine) as db, db.begin():
            account = target(db, env)
    identity = queue(env, account).task_id
    with Session(engine) as db, db.begin():
        dist = db.get(MaterialDistribution, identity)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        dist.status = op.status = "result_unknown"
        op.remote_response = {
            **op.remote_response,
            "transport": "url_relay",
            "send_armed": True,
            "error_code": "old_remote_error",
            **({"video_id": "known-target"} if known else {}),
            **({"reconciliation_budget": budget} if budget else {}),
        }
    return identity


def page(number, total=5):
    return {
        "list": [{"video_id": "unrelated", "file_name": "other.mp4"}],
        "page_info": {"page": number, "page_size": 100, "total_page": total},
    }


def stopped(identity):
    dist, op, _ = state(identity)
    assert dist.status == op.status == "result_unknown"
    assert dist.reason_code == "material_reconciliation_budget_exhausted"
    assert op.remote_response.get("reconciliation_stopped") is True
    assert not op.remote_response.get("reconciliation_complete")
    assert op.remote_response["send_armed"] is True
    assert op.attempt_token is None and op.claimed_until is None


@pytest.mark.parametrize("response", ["empty", "error"])
def test_three_nonprogress_reads_stop_and_old_messages_cannot_restart(
    source_env, redis_client, wire, response
):
    identity = unknown(source_env)
    for _ in range(3):
        wire[1].append(
            {"list": []}
            if response == "empty"
            else ReadTimeoutError(None, "https://offline.invalid", "timeout")
        )
        run(source_env, redis_client, identity, read_only=True)
    stopped(identity)
    before = len(wire[0])
    run(source_env, redis_client, identity, read_only=True)
    run(source_env, redis_client, identity, kind="prepare")
    assert len(wire[0]) == before == 3


def test_valid_pages_progress_immediately_and_clear_old_error(
    source_env, redis_client, wire
):
    identity = unknown(source_env, known=False)
    for number in range(1, 5):
        wire[1].append(page(number))
        run(source_env, redis_client, identity, read_only=True)
        dist, op, _ = state(identity)
        assert not op.remote_response.get("reconciliation_stopped")
        assert op.remote_response.get("error_code") is None
        assert op.remote_response["search_page"] == number + 1
        with Session(engine) as db:
            dispatch = db.exec(
                select(PendingDispatch).where(
                    PendingDispatch.payload["revision"].as_integer()
                    == op.remote_response["revision"],
                    PendingDispatch.payload["distribution_id"].as_string()
                    == str(identity),
                )
            ).one()
            assert dispatch.available_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "budget",
    [
        {
            "started_at": (datetime.now(UTC) - timedelta(minutes=16)).isoformat(),
            "claims": 1,
            "no_progress": 0,
        },
        {"started_at": datetime.now(UTC).isoformat(), "claims": 120, "no_progress": 0},
    ],
)
def test_expired_round_does_not_make_another_remote_call(
    source_env, redis_client, wire, budget
):
    identity = unknown(source_env, budget=budget)
    run(source_env, redis_client, identity, read_only=True)
    stopped(identity)
    assert wire[0] == []


def test_stopped_round_cannot_queue_or_repair_but_explicit_reconcile_reopens(
    source_env, redis_client, wire
):
    identity = unknown(
        source_env,
        budget={
            "started_at": datetime.now(UTC).isoformat(),
            "claims": 120,
            "no_progress": 0,
        },
    )
    run(source_env, redis_client, identity, read_only=True)
    stopped(identity)
    with Session(engine) as db, db.begin():
        dist = db.get(MaterialDistribution, identity)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        original_id, original_digest = op.id, op.request_digest
        for dispatch in db.exec(select(PendingDispatch)).all():
            dispatch.published_at = dispatch.available_at = datetime.now(
                UTC
            ) - timedelta(minutes=5)
        db.flush()
        assert repair_material_dispatches(db) == 0
        before = set(db.exec(select(PendingDispatch.id)).all())
        queue_distribution(db, dist, op, kind="verify", read_only=True)
        assert set(db.exec(select(PendingDispatch.id)).all()) == before
        queue_distribution(db, dist, op, kind="verify", read_only=True, observe=True)
    wire[1].append({"list": []})
    run(source_env, redis_client, identity, read_only=True)
    dist, op, _ = state(identity)
    assert not op.remote_response.get("reconciliation_stopped")
    assert op.id == original_id and op.request_digest == original_digest
    assert op.remote_response["video_id"] == "known-target"
    assert op.remote_response["reconciliation_budget"]["claims"] == 1
    assert len(wire[0]) == 1 and wire[0][0][0] == "GET"


def test_admission_wait_does_not_consume_no_progress_but_deadline_still_stops(
    source_env, redis_client, wire
):
    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.materials import sdk_assets as api

    identity = unknown(
        source_env,
        budget={
            "started_at": datetime.now(UTC).isoformat(),
            "claims": 2,
            "no_progress": 2,
        },
    )
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": api.INFO_ENDPOINT,
        "tenant_id": source_env["context"].tenant_id,
        "advertiser_id": "target-account",
        "lease_id": uuid4(),
    }
    assert admit_call(
        redis_client, **scope, policy=admission_policy(api.INFO_ENDPOINT)
    ).granted
    try:
        for _ in range(4):
            run(source_env, redis_client, identity, read_only=True)
            _, op, _ = state(identity)
            assert not op.remote_response.get("reconciliation_stopped")
            assert op.remote_response["reconciliation_budget"]["no_progress"] == 2
        assert op.remote_response["reconciliation_budget"]["claims"] == 6
        with Session(engine) as db, db.begin():
            op = db.get(MaterialAssetOperation, op.id)
            op.remote_response = {
                **op.remote_response,
                "reconciliation_budget": {
                    **op.remote_response["reconciliation_budget"],
                    "started_at": (
                        datetime.now(UTC) - timedelta(minutes=16)
                    ).isoformat(),
                },
            }
        run(source_env, redis_client, identity, read_only=True)
        stopped(identity)
        assert wire[0] == []
    finally:
        release_call(redis_client, **scope)


def verified_response():
    response = info(vid="known-target")
    response["list"][0].update(
        size=len(CONTENT), width=720, height=1280, duration=10, format="mp4"
    )
    return response


def test_last_allowed_claim_can_confirm_positive_receipt(
    source_env, redis_client, wire
):
    identity = unknown(
        source_env,
        budget={
            "started_at": datetime.now(UTC).isoformat(),
            "claims": 119,
            "no_progress": 2,
        },
    )
    wire[1].append(verified_response())
    run(source_env, redis_client, identity, read_only=True)
    dist, op, mapping = state(identity)
    assert dist.status == "ready" and op.status == "succeeded"
    assert mapping.video_id == "known-target"
    assert op.remote_response.get("error_code") is None
    assert not op.remote_response.get("reconciliation_stopped")
    assert op.remote_response["reconciliation_budget"]["claims"] == 120


def test_explicit_recheck_of_old_success_starts_fresh_read_budget(
    source_env, redis_client, wire
):
    identity = unknown(source_env)
    with Session(engine) as db, db.begin():
        dist = db.get(MaterialDistribution, identity)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        dist.status, op.status = "ready", "succeeded"
        op.remote_response = {
            **op.remote_response,
            "upload_video_id": "known-target",
            "reconciliation_budget": {
                "started_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "claims": 120,
                "no_progress": 0,
            },
        }
        queue_distribution(db, dist, op, kind="verify", read_only=True, observe=True)
    wire[1].append(verified_response())
    run(source_env, redis_client, identity, read_only=True)
    dist, op, mapping = state(identity)
    assert dist.status == "ready" and mapping.video_id == "known-target"
    assert op.remote_response["upload_video_id"] == "known-target"
    assert op.remote_response["reconciliation_budget"]["claims"] == 1


def test_progress_resets_failure_streak_and_vid_discovery_is_immediate(
    source_env, redis_client, wire
):
    from app.modules.materials.models import MaterialFile
    from app.modules.materials.source_uploads import remote_name

    identity = unknown(
        source_env,
        known=False,
        budget={
            "started_at": datetime.now(UTC).isoformat(),
            "claims": 2,
            "no_progress": 2,
        },
    )
    with Session(engine) as db:
        name = remote_name(db.get(MaterialFile, source_env["material_id"]))
    response = verified_response()
    response["list"][0]["file_name"] = name
    response["page_info"] = {"page": 1, "page_size": 100, "total_page": 1}
    wire[1].append(response)
    run(source_env, redis_client, identity, read_only=True)
    _, op, _ = state(identity)
    assert op.remote_response["video_id"] == "known-target"
    assert op.remote_response["reconciliation_budget"]["no_progress"] == 0
    assert op.remote_response.get("error_code") is None
    with Session(engine) as db:
        dispatch = db.exec(
            select(PendingDispatch).where(
                PendingDispatch.payload["revision"].as_integer()
                == op.remote_response["revision"],
                PendingDispatch.payload["distribution_id"].as_string() == str(identity),
            )
        ).one()
        assert dispatch.available_at <= datetime.now(UTC)
    wire[1].append({"list": []})
    run(source_env, redis_client, identity, read_only=True)
    assert (
        state(identity)[1].remote_response["reconciliation_budget"]["no_progress"] == 1
    )


def unknown_peers(env, count):
    from app.modules.materials.models import MaterialFile

    first = unknown(env)
    identities = [first]
    for index in range(1, count):
        with Session(engine) as db, db.begin():
            material = db.get(MaterialFile, env["material_id"])
            identity = uuid4()
            db.add(
                MaterialFile(
                    **{
                        **material.model_dump(),
                        "id": identity,
                        "object_key": str(identity),
                    }
                )
            )
        dist_id = unknown({**env, "material_id": identity}, account="target-account")
        with Session(engine) as db, db.begin():
            dist = db.get(MaterialDistribution, dist_id)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            op.remote_response = {
                **op.remote_response,
                "video_id": f"known-peer-{index}",
            }
        identities.append(dist_id)
    return identities


@pytest.mark.parametrize("response", ["empty", "error"])
def test_automatic_batch_stops_both_members_after_three_no_progress_reads(
    source_env, redis_client, wire, response
):
    identities = unknown_peers(source_env, 2)
    for _ in range(3):
        wire[1].append(
            {"list": []}
            if response == "empty"
            else ReadTimeoutError(None, "https://offline.invalid", "timeout")
        )
        run(source_env, redis_client, identities[0])
    for identity in identities:
        stopped(identity)
        run(source_env, redis_client, identity)
    assert len(wire[0]) == 3


def test_automatic_batch_does_not_claim_stopped_sibling(source_env, redis_client, wire):
    first, second, stopped_id = unknown_peers(source_env, 3)
    with Session(engine) as db, db.begin():
        dist = db.get(MaterialDistribution, stopped_id)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        op.remote_response = {**op.remote_response, "reconciliation_stopped": True}
        snapshot = dict(op.remote_response)
    wire[1].append({"list": []})
    run(source_env, redis_client, first)
    assert state(stopped_id)[1].remote_response == snapshot
    for identity in [first, second]:
        assert (
            state(identity)[1].remote_response["reconciliation_budget"]["claims"] == 1
        )
    assert len(wire[0]) == 1


def test_automatic_batch_checks_expired_budget_under_member_locks(
    source_env, redis_client, wire
):
    identities = unknown_peers(source_env, 2)
    with Session(engine) as db, db.begin():
        for identity in identities:
            dist = db.get(MaterialDistribution, identity)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            op.remote_response = {
                **op.remote_response,
                "reconciliation_budget": {
                    "started_at": (
                        datetime.now(UTC) - timedelta(minutes=16)
                    ).isoformat(),
                    "claims": 1,
                    "no_progress": 0,
                },
            }
    run(source_env, redis_client, identities[0])
    for identity in identities:
        stopped(identity)
    assert wire[0] == []


@pytest.mark.parametrize(
    "response_kind",
    ["empty", "unavailable", "content_mismatch", "network", "admission"],
)
def test_batch_invalidates_only_explicit_negative_target_evidence(
    source_env, redis_client, wire, response_kind
):
    from app.jobs.admission import admission_policy, admit_call, release_call
    from app.modules.materials import sdk_assets as api
    from tests.modules.materials.test_readiness import asset

    identities = unknown_peers(source_env, 2)
    records = []
    with Session(engine) as db, db.begin():
        for identity in identities:
            dist = db.get(MaterialDistribution, identity)
            op = db.get(MaterialAssetOperation, dist.operation_id)
            mapping = asset(
                db, {**source_env, "material_id": dist.material_id}, "target-account"
            )
            mapping.video_id = op.remote_response["video_id"]
            record = verified_response()["list"][0]
            record["video_id"] = mapping.video_id
            if response_kind == "unavailable":
                record["displayable"] = False
            if response_kind == "content_mismatch":
                record["signature"] = "0" * 32
            records.append(record)
    scope = {
        "app_scope": settings.TIKTOK_APP_ID,
        "endpoint": api.INFO_ENDPOINT,
        "tenant_id": source_env["context"].tenant_id,
        "advertiser_id": "target-account",
        "lease_id": uuid4(),
    }
    if response_kind == "admission":
        assert admit_call(
            redis_client, **scope, policy=admission_policy(api.INFO_ENDPOINT)
        ).granted
    elif response_kind == "network":
        wire[1].append(ReadTimeoutError(None, "https://offline.invalid", "timeout"))
    else:
        wire[1].append({"list": [] if response_kind == "empty" else records})
    try:
        run(source_env, redis_client, identities[0])
        for identity in identities:
            dist, op, mapping = state(identity)
            assert mapping.status == (
                "available"
                if response_kind in {"network", "admission"}
                else "result_unknown"
            )
            assert mapping.video_id == op.remote_response["video_id"]
            assert dist.status == "verifying"
    finally:
        if response_kind == "admission":
            release_call(redis_client, **scope)
