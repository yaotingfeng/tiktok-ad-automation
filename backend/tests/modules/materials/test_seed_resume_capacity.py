"""真实 PG 的20×10已转存矩阵恢复；防止共享前逐关系重复鉴权拖慢整批。"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.accounts.models import BCAccountAccess
from app.modules.materials import bc_seeding
from app.modules.materials.content_identity import content_key
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from app.modules.materials.seed_models import MaterialBCSeed
from tests.modules.materials.test_bc_seeding import seed_env as seed_env
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_seed_batch_resume import complete_seed
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture(autouse=True)
def retain_build_history(isolated_strategy_database, monkeypatch):
    """整条转存→共享用真实临时库，结束时删除临时库而不篡改不可变账本。"""
    db = isolated_strategy_database[0]
    for module in (
        "test_seed_resume_capacity",
        "test_source_uploads",
        "test_url_ingest",
        "test_remote_only_distribution",
        "test_bc_seeding",
        "test_distribution",
        "test_seed_batch_resume",
    ):
        monkeypatch.setattr(f"tests.modules.materials.{module}.engine", db)
    return db


def ready_matrix(env, redis_client, wire):
    """先跑一条正式转存，再复制为不同内容的历史已成功种子测试初态。"""
    with Session(engine) as db, db.begin():
        accounts = [env["target"]] + [
            target(db, env, advertiser_id=f"capacity-{index}") for index in range(9)
        ]
    consumers = [queue(env, account).task_id for account in accounts]
    owner_id = complete_seed(env, redis_client, wire)
    with Session(engine) as db, db.begin():
        material = db.get(MaterialFile, env["material_id"])
        owner = db.get(MaterialDistribution, owner_id)
        owner_op = db.get(MaterialAssetOperation, owner.operation_id)
        source = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == material.id,
                AccountMaterial.bc_id == env["bc_id"],
                AccountMaterial.advertiser_id == env["primary"],
            )
        ).one()
        seed = db.exec(
            select(MaterialBCSeed).where(MaterialBCSeed.distribution_id == owner_id)
        ).one()
        templates = [
            (db.get(MaterialDistribution, identity), state(identity)[1])
            for identity in consumers
        ]
        for index in range(1, 20):
            identity = uuid4()
            copied = MaterialFile(
                **(
                    material.model_dump()
                    | {
                        "id": identity,
                        "object_key": f"capacity/{identity}",
                        "sha256": f"{index:064x}",
                        "video_md5": f"{index:032x}",
                    }
                )
            )
            db.add(copied)
            db.flush()
            copied_op = MaterialAssetOperation(
                **(
                    owner_op.model_dump()
                    | {
                        "id": uuid4(),
                        "material_id": identity,
                        "remote_response": {"video_id": f"capacity-vid-{index}"},
                    }
                )
            )
            db.add(copied_op)
            db.flush()
            copied_owner = MaterialDistribution(
                **(
                    owner.model_dump()
                    | {
                        "id": uuid4(),
                        "material_id": identity,
                        "operation_id": copied_op.id,
                    }
                )
            )
            db.add(copied_owner)
            db.flush()
            copied_seed = MaterialBCSeed(
                **(
                    seed.model_dump()
                    | {
                        "id": uuid4(),
                        "material_id": identity,
                        "distribution_id": copied_owner.id,
                        "content_key": content_key(copied),
                    }
                )
            )
            db.add(copied_seed)
            db.add(
                AccountMaterial(
                    **(
                        source.model_dump()
                        | {
                            "id": uuid4(),
                            "material_id": identity,
                            "video_id": f"capacity-vid-{index}",
                            "mid": f"capacity-mid-{index}",
                        }
                    )
                )
            )
            db.flush()
            for original_dist, original_op in templates:
                op = MaterialAssetOperation(
                    **(
                        original_op.model_dump()
                        | {
                            "id": uuid4(),
                            "material_id": identity,
                        }
                    )
                )
                db.add(op)
                db.flush()
                dist = MaterialDistribution(
                    **(
                        original_dist.model_dump()
                        | {
                            "id": uuid4(),
                            "material_id": identity,
                            "operation_id": op.id,
                            "seed_id": copied_seed.id,
                            "source_material_id": identity,
                        }
                    )
                )
                db.add(dist)
                consumers.append(dist.id)
    return consumers


def test_twenty_by_ten_seed_binding_has_bounded_sql_and_preserves_common_source(
    seed_env, redis_client, wire
):
    consumers = ready_matrix(seed_env, redis_client, wire)
    assert len(consumers) == 200
    before = len(wire[0])
    statements = []

    def record(conn, _cursor, statement, _params, _context, _many):
        if conn.engine.url.database == engine.url.database:
            statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    started = perf_counter()
    try:
        changed = bc_seeding.resume_ready_seed_dependents(
            database_engine=engine,
            context=seed_env["context"],
            distribution_id=consumers[0],
        )
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    elapsed = perf_counter() - started
    assert changed == 200
    assert len(wire[0]) == before, "本地绑定不能提前发送共享或重复转存"
    with Session(engine) as db:
        rows = db.exec(
            select(MaterialDistribution, MaterialAssetOperation)
            .join(
                MaterialAssetOperation,
                MaterialAssetOperation.id == MaterialDistribution.operation_id,
            )
            .where(MaterialDistribution.id.in_(consumers))
        ).all()
        assert len(rows) == 200
        assert all(
            op.status == "pending" and dist.status == "queued" for dist, op in rows
        )
        assert all(op.remote_response["transport"] == "native_share" for _, op in rows)
        assert all(
            op.remote_response["source_advertiser_id"] == "primary-b" for _, op in rows
        )
    print(f"seed_resume_capacity: sql={len(statements)}, seconds={elapsed:.3f}")  # noqa: T201
    # 200关系只应线性处理身份与映射，不重复展开共同权限树。
    assert len(statements) <= 4000


def test_concurrent_seed_binding_claims_each_relationship_once(
    seed_env, redis_client, wire
):
    consumers = ready_matrix(seed_env, redis_client, wire)
    barrier = Barrier(2)

    def resume():
        barrier.wait(timeout=5)
        return bc_seeding.resume_ready_seed_dependents(
            database_engine=engine,
            context=seed_env["context"],
            distribution_id=consumers[0],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == [0, 200]


def test_peer_finishes_between_candidate_queries_is_noop(seed_env, redis_client, wire):
    consumers = ready_matrix(seed_env, redis_client, wire)
    finished = []

    def finish_peer(conn, _cursor, statement, _params, _context, _many):
        if conn.engine.url.database != engine.url.database:
            return
        if finished or not statement.startswith(
            "SELECT DISTINCT material_distribution.material_id"
        ):
            return
        finished.append(True)
        assert (
            bc_seeding.resume_ready_seed_dependents(
                database_engine=engine,
                context=seed_env["context"],
                distribution_id=consumers[0],
            )
            == 200
        )

    event.listen(Engine, "after_cursor_execute", finish_peer)
    try:
        assert (
            bc_seeding.resume_ready_seed_dependents(
                database_engine=engine,
                context=seed_env["context"],
                distribution_id=consumers[0],
            )
            == 0
        )
    finally:
        event.remove(Engine, "after_cursor_execute", finish_peer)
    assert finished


def test_revoked_target_does_not_poison_other_nine_accounts(
    seed_env, redis_client, wire
):
    consumers = ready_matrix(seed_env, redis_client, wire)
    with Session(engine) as db, db.begin():
        grant = db.get(
            BCAccountAccess,
            (
                seed_env["context"].tenant_id,
                seed_env["bc_id"],
                seed_env["target"],
                seed_env["connection_id"],
            ),
        )
        grant.can_build = False
    assert (
        bc_seeding.resume_ready_seed_dependents(
            database_engine=engine,
            context=seed_env["context"],
            distribution_id=consumers[0],
        )
        == 180
    )
    with Session(engine) as db:
        rows = db.exec(
            select(MaterialDistribution).where(MaterialDistribution.id.in_(consumers))
        ).all()
        assert sum(dist.status == "blocked" for dist in rows) == 20
        assert all(
            dist.status
            == ("blocked" if dist.advertiser_id == seed_env["target"] else "queued")
            for dist in rows
        )


def test_ready_matrix_reaches_one_common_share_request(seed_env, redis_client, wire):
    import json

    consumers = ready_matrix(seed_env, redis_client, wire)
    with Session(engine) as db:
        sources = db.exec(
            select(AccountMaterial, MaterialFile)
            .join(MaterialFile, MaterialFile.id == AccountMaterial.material_id)
            .where(
                AccountMaterial.tenant_id == seed_env["context"].tenant_id,
                AccountMaterial.advertiser_id == seed_env["primary"],
            )
        ).all()
        records = [
            info(
                vid=asset.video_id,
                material_id=asset.mid,
                signature=material.video_md5,
                file_name=f"seed-{material.id}.mp4",
            )["list"][0]
            for asset, material in sources
        ]
    assert len(records) == 20
    wire[1].extend([{"list": records}, {}])
    run(seed_env, redis_client, consumers[0], kind="prepare")
    assert all(state(identity)[0].status == "verifying" for identity in consumers)
    shares = [json.loads(call[2]["body"]) for call in wire[0] if "/share/" in call[1]]
    assert len(shares) == 1
    assert shares[0]["advertiser_id"] == "primary-b"
    assert len(shares[0]["material_ids"]) == 20
    assert len(shares[0]["shared_advertiser_ids"]) == 10


def test_mid_binding_revocation_rolls_back_that_account_only(
    seed_env, redis_client, wire
):
    consumers = ready_matrix(seed_env, redis_client, wire)
    revoked = []

    def revoke(conn, _cursor, statement, _params, _context, _many):
        if conn.engine.url.database != engine.url.database:
            return
        if revoked or not statement.startswith("UPDATE material_asset_operation"):
            return
        revoked.append(True)
        with Session(engine) as db, db.begin():
            grant = db.get(
                BCAccountAccess,
                (
                    seed_env["context"].tenant_id,
                    seed_env["bc_id"],
                    "capacity-0",
                    seed_env["connection_id"],
                ),
            )
            grant.can_build = False

    event.listen(Engine, "before_cursor_execute", revoke)
    try:
        changed = bc_seeding.resume_ready_seed_dependents(
            database_engine=engine,
            context=seed_env["context"],
            distribution_id=consumers[0],
        )
    finally:
        event.remove(Engine, "before_cursor_execute", revoke)
    assert revoked and changed == 180
    for identity in consumers:
        dist, op, _ = state(identity)
        if dist.advertiser_id == "capacity-0":
            assert dist.status == "blocked" and op.status == "failed"
            assert op.remote_response.get("transport") is None
            assert op.remote_response["definite_no_effect"] is True
        else:
            assert op.remote_response["transport"] == "native_share"


def test_seed_row_lock_exits_within_budget_and_same_dependencies_resume(
    seed_env, redis_client, wire
):
    consumers = ready_matrix(seed_env, redis_client, wire)
    before = len(wire[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as blocker, blocker.begin():
            blocker.exec(
                select(MaterialFile)
                .where(MaterialFile.id == seed_env["material_id"])
                .with_for_update()
            ).one()
            future = pool.submit(
                bc_seeding.resume_ready_seed_dependents,
                database_engine=engine,
                context=seed_env["context"],
                distribution_id=consumers[0],
            )
            try:
                with pytest.raises(DomainError) as caught:
                    future.result(timeout=7)
                assert caught.value.code == "tiktok_local_resources_unavailable"
                assert caught.value.retryable
            except FutureTimeoutError:
                pytest.fail("种子绑定不应持锁等待到 Worker 硬超时")
        for identity in consumers:
            dist, op, _ = state(identity)
            assert dist.status == "queued" and op.status == "pending"
            assert op.remote_response.get("transport") is None
        assert (
            bc_seeding.resume_ready_seed_dependents(
                database_engine=engine,
                context=seed_env["context"],
                distribution_id=consumers[0],
            )
            == 200
        )
    assert len(wire[0]) == before
