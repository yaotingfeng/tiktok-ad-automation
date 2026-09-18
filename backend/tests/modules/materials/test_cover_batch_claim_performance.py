"""真实 PostgreSQL 的共享领取预算及安全围栏，不替换数据库或执行器。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials import cover_sharing, covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import job_state
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


@pytest.mark.parametrize("sql_latency", [0, 0.004])
def test_twenty_by_ten_claim_sql_budget_and_complete_flow(
    source_env, redis_client, wire, monkeypatch, sql_latency, record_property
):
    identities = matrix(source_env, 20, 10)
    statements = []
    elapsed = []
    prepare = cover_sharing._prepare

    def observed_prepare(*args, **kwargs):
        def observe(_connection, _cursor, statement, *_):
            statements.append(statement)
            if sql_latency:
                # 模拟繁忙数据库每条SQL仅4ms往返；保留真实SQL、锁和5秒本地预算。
                sleep(sql_latency)

        start = monotonic()
        event.listen(Engine, "before_cursor_execute", observe)
        try:
            return prepare(*args, **kwargs)
        finally:
            event.remove(Engine, "before_cursor_execute", observe)
            elapsed.append(monotonic() - start)

    monkeypatch.setattr(cover_sharing, "_prepare", observed_prepare)
    wire[1].extend(
        [
            {"list": [image(n) for n in range(20)]},
            *[page([]) for _ in range(10)],
            {"failed_infos": {}},
        ]
    )
    drive(source_env, redis_client, identities[0])
    posts = [json.loads(call[2]["body"]) for call in wire[0] if call[0] == "POST"]
    assert len(posts) == 1
    assert len(posts[0]["material_ids"]) == 20
    assert len(posts[0]["shared_advertiser_ids"]) == 10
    wire[1].extend(
        page([image(n, target_id=True) for n in range(20)]) for _ in range(10)
    )
    drive(source_env, redis_client, identities[0], read=True)
    assert all(job_state(identity).status == "READY" for identity in identities)
    assert all(
        job_state(identity).known_image_id.startswith("tos-target-")
        for identity in identities
    )
    # 不能随200成员逐项读取job/dispatch/MD5/映射；真实bounded_session仍只给5秒。
    assert len(elapsed) == 1
    record_property("prepare_sql_count", len(statements))
    record_property("prepare_elapsed_seconds", elapsed[0])
    assert elapsed[0] < 5
    assert len(statements) <= 500, (len(statements), elapsed)
    assert sum("FROM pending_dispatch" in sql for sql in statements) <= 1


@pytest.mark.parametrize(
    "change", ["revoke", "claim", "dispatch", "payload", "revision", "md5", "mapping"]
)
def test_selected_member_changes_roll_back_whole_rectangle(
    source_env, monkeypatch, wire, change
):
    identities = matrix(source_env, 2, 2)
    first, peer = job_state(identities[0]), job_state(identities[-1])
    claimed = covers._claim(
        engine,
        source_env["context"],
        first.id,
        first.dispatch_id,
        first.revision,
        read=False,
    )
    assert claimed is not None
    rectangle = cover_sharing.rectangle
    competitor_claims = []

    def change_after_selection(*args):
        selected = rectangle(*args)
        if change == "claim":
            # 独立连接的真实竞争者先领取成功，旧候选不能覆盖它的新nonce。
            with ThreadPoolExecutor(max_workers=1) as pool:
                other = pool.submit(
                    covers._claim,
                    engine,
                    source_env["context"],
                    peer.id,
                    peer.dispatch_id,
                    peer.revision,
                    read=False,
                ).result(timeout=5)
            assert other is not None
            competitor_claims.append(other[1])
        else:
            with Session(engine) as db, db.begin():
                job = db.get(MaterialCoverJob, peer.id)
                assert job is not None
                if change == "revoke":
                    grant = db.get(
                        BCAccountAccess,
                        (
                            job.tenant_id,
                            job.bc_id,
                            job.advertiser_id,
                            job.connection_id,
                        ),
                    )
                    assert grant is not None
                    grant.can_upload = False
                elif change == "dispatch":
                    dispatch = db.get(PendingDispatch, job.dispatch_id)
                    assert dispatch is not None
                    dispatch.task_key = "stale-cover-key"
                elif change == "payload":
                    dispatch = db.get(PendingDispatch, job.dispatch_id)
                    assert dispatch is not None
                    dispatch.payload = {
                        "job_id": str(uuid4()),
                        "revision": job.revision,
                    }
                elif change == "revision":
                    job.revision += 1
                elif change == "md5":
                    material = db.get(MaterialFile, job.material_id)
                    assert material is not None
                    material.video_md5 = "f" * 32
                elif change == "mapping":
                    asset = db.get(AccountMaterial, job.asset_id)
                    assert asset is not None
                    asset.video_id = "changed-vid"
        return selected

    monkeypatch.setattr(cover_sharing, "rectangle", change_after_selection)
    with pytest.raises(DomainError, match="封面成员已被领取"):
        with (
            bounded_session(
                engine, task_deadline=datetime.now(UTC) + timedelta(seconds=30)
            ) as db,
            db.begin(),
        ):
            cover_sharing._prepare(db, source_env["context"], *claimed)
    assert wire[0] == []
    assert all(job_state(identity).share_batch_id is None for identity in identities)
    assert all(job_state(identity).request_armed_at is None for identity in identities)
    assert job_state(identities[1]).status == "PENDING"
    if competitor_claims:
        assert job_state(peer.id).claim_token == competitor_claims[0]


def test_peer_claim_contention_requeues_anchor_without_remote_send(
    source_env, redis_client, monkeypatch, wire
):
    identities = matrix(source_env, 2, 2)
    first, peer = job_state(identities[0]), job_state(identities[-1])
    claimed = covers._claim(
        engine,
        source_env["context"],
        first.id,
        first.dispatch_id,
        first.revision,
        read=False,
    )
    assert claimed is not None
    rectangle = cover_sharing.rectangle
    competitor_claims = []

    def claim_peer_after_selection(*args):
        selected = rectangle(*args)
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(
                covers._claim,
                engine,
                source_env["context"],
                peer.id,
                peer.dispatch_id,
                peer.revision,
                read=False,
            ).result(timeout=5)
        assert other is not None
        competitor_claims.append(other[1])
        return selected

    monkeypatch.setattr(cover_sharing, "rectangle", claim_peer_after_selection)
    cover_sharing.run_shared_cover(
        engine,
        redis_client,
        source_env["context"],
        *claimed,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )

    anchor = job_state(first.id)
    assert anchor.status == "PENDING"
    assert anchor.error_code is None
    assert anchor.dispatch_id is not None
    assert anchor.request_armed_at is None and anchor.share_batch_id is None
    assert job_state(peer.id).claim_token == competitor_claims[0]
    assert wire[0] == []


def test_old_anchor_claim_never_plans_batch(source_env, wire):
    identities = matrix(source_env, 2, 2)
    first = job_state(identities[0])
    claimed = covers._claim(
        engine,
        source_env["context"],
        first.id,
        first.dispatch_id,
        first.revision,
        read=False,
    )
    assert claimed is not None
    with pytest.raises(DomainError):
        with Session(engine) as db, db.begin():
            cover_sharing._prepare(db, source_env["context"], claimed[0], uuid4())
    assert wire[0] == []
    assert all(job_state(identity).share_batch_id is None for identity in identities)


def test_planning_source_upload_permission_cache_is_not_reused_for_post(
    source_env, redis_client, wire, monkeypatch
):
    identities = matrix(source_env, 2, 2)
    prepare = cover_sharing._prepare

    def revoke_after_planning(db, context, first, nonce):
        prepared = prepare(db, context, first, nonce)
        assert prepared is not None
        grant = db.get(
            BCAccountAccess,
            (context.tenant_id, first.bc_id, "actual-account", first.connection_id),
        )
        grant.can_upload = False
        return prepared

    monkeypatch.setattr(cover_sharing, "_prepare", revoke_after_planning)
    wire[1].extend([{"list": [image(0), image(1)]}, page([]), page([])])
    drive(source_env, redis_client, identities[0])
    # read权限仍有效，库存读回允许；撤销的upload必须在物理POST前重新拒绝。
    assert len(wire[0]) == 3
    assert all(call[0] == "GET" for call in wire[0])
    assert wire[1] == []
    assert all(job_state(identity).request_armed_at is None for identity in identities)
    assert all(job_state(identity).status != "READY" for identity in identities)
