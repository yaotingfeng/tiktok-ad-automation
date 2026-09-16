"""首次转存重新准备保留旧任务，并使用真实 PG/Redis 验证恢复边界。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import BCAccountAccess, TenantBC
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from app.modules.materials.seed_models import MaterialBCSeed
from tests.modules.materials.test_bc_seeding import seed_env as seed_env
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import read, target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


def seeds(env):
    with Session(engine) as db:
        return db.exec(
            select(MaterialBCSeed).where(
                MaterialBCSeed.tenant_id == env["context"].tenant_id
            )
        ).all()


def source_authorized(env, authorized):
    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                env["context"].tenant_id,
                env["source_bc_id"],
                "actual-account",
                env["connection_id"],
            ),
        ).authorized = authorized


def complete_seed(env, redis_client, wire, seed):
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(env, redis_client, seed.distribution_id, kind="prepare")
    wire[1].append(info(vid="primary-vid", material_id="primary-mid"))
    run(env, redis_client, seed.distribution_id)
    assert state(seed.distribution_id)[0].status == "ready"


@pytest.mark.parametrize("rebind", [None, "source", "target"])
def test_new_prepare_after_definite_unsent_failure_preserves_old_jobs(
    seed_env, redis_client, wire, rebind
):
    first = queue(seed_env, seed_env["target"])
    old = seeds(seed_env)[0]
    source_authorized(seed_env, False)
    run(seed_env, redis_client, old.distribution_id, kind="prepare")
    run(seed_env, redis_client, first.task_id, kind="prepare")
    old_dist, old_op, _ = state(old.distribution_id)
    assert old_dist.status == "blocked" and old_op.remote_response["definite_no_effect"]
    source_authorized(seed_env, True)
    with Session(engine) as db, db.begin():
        other = target(db, seed_env, advertiser_id="target-b2")
        if rebind:
            bc_id = (
                seed_env["source_bc_id"] if rebind == "source" else seed_env["bc_id"]
            )
            db.get(
                BCConnectionBinding,
                (seed_env["context"].tenant_id, bc_id, seed_env["connection_id"]),
            ).revision += 1
    assert read(seed_env, other).state == "preparable"
    second = queue(seed_env, other)
    rows = seeds(seed_env)
    assert len(rows) == 2, (
        "A settled no-effect owner must not permanently poison new preparation"
    )
    new = next(row for row in rows if row.id != old.id)
    assert new.advertiser_id == old.advertiser_id == "primary-b"
    assert state(second.task_id)[0].seed_id == new.id
    complete_seed(seed_env, redis_client, wire, new)
    run(seed_env, redis_client, old.distribution_id, kind="prepare")
    run(seed_env, redis_client, first.task_id, kind="prepare")
    current_dist, current_op, _ = state(old.distribution_id)
    assert current_dist.model_dump() == old_dist.model_dump()
    assert current_op.model_dump() == old_op.model_dump()
    assert state(first.task_id)[0].seed_id == old.id
    assert state(first.task_id)[0].status == "blocked"
    assert len([call for call in wire[0] if "/video/ad/upload/" in call[1]]) == 1


def test_target_rebind_before_send_releases_only_unsent_operation_for_new_jobs(
    seed_env, redis_client, wire
):
    queue(seed_env, seed_env["target"])
    old = seeds(seed_env)[0]
    with Session(engine) as db, db.begin():
        db.get(
            BCConnectionBinding,
            (
                seed_env["context"].tenant_id,
                seed_env["bc_id"],
                seed_env["connection_id"],
            ),
        ).revision += 1
        other = target(db, seed_env, advertiser_id="target-b2")
    run(seed_env, redis_client, old.distribution_id, kind="prepare")
    dist, operation, _ = state(old.distribution_id)
    assert dist.status == "blocked"
    assert (
        operation.status == "failed" and operation.remote_response["definite_no_effect"]
    )
    prepared = queue(seed_env, other)
    new = next(row for row in seeds(seed_env) if row.id != old.id)
    assert state(prepared.task_id)[0].seed_id == new.id
    assert state(old.distribution_id)[1].frozen_route == operation.frozen_route
    assert wire[0] == []


def test_parallel_alias_consumers_share_one_new_preparation_after_unsent_failure(
    seed_env, redis_client, wire
):
    delayed_old_waiter = queue(seed_env, seed_env["target"])
    old = seeds(seed_env)[0]
    source_authorized(seed_env, False)
    run(seed_env, redis_client, old.distribution_id, kind="prepare")
    source_authorized(seed_env, True)
    consumers = []
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, seed_env["material_id"])
        for index in range(3):
            alias = MaterialFile(
                **(original.model_dump() | {"id": uuid4(), "object_key": str(uuid4())})
            )
            db.add(alias)
            consumers.append(
                (
                    {**seed_env, "material_id": alias.id},
                    target(db, seed_env, advertiser_id=f"new-{index}"),
                )
            )
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda pair: queue(*pair), consumers))
    rows = seeds(seed_env)
    assert len(rows) == 2
    new = next(row for row in rows if row.id != old.id)
    assert {state(result.task_id)[0].seed_id for result in results} == {new.id}
    complete_seed(seed_env, redis_client, wire, new)
    run(seed_env, redis_client, delayed_old_waiter.task_id, kind="prepare")
    delayed_dist, delayed_operation, delayed_mapping = state(delayed_old_waiter.task_id)
    assert delayed_dist.status == "blocked" and delayed_operation.status == "failed"
    assert delayed_dist.seed_id == old.id and delayed_mapping is None
    assert len([call for call in wire[0] if "/video/ad/upload/" in call[1]]) == 1


def test_explicit_no_effect_rejection_allows_new_generation_on_same_primary(
    seed_env, redis_client, wire
):
    queue(seed_env, seed_env["target"])
    old = seeds(seed_env)[0]
    source_authorized(seed_env, False)
    run(seed_env, redis_client, old.distribution_id, kind="prepare")
    source_authorized(seed_env, True)
    with Session(engine) as db, db.begin():
        owner = db.get(MaterialDistribution, old.distribution_id)
        operation = db.get(MaterialAssetOperation, owner.operation_id)
        operation.remote_response = {**operation.remote_response, "send_armed": True}
        other = target(db, seed_env, advertiser_id="target-b2")
        db.get(
            TenantBC, (seed_env["context"].tenant_id, seed_env["bc_id"])
        ).material_advertiser_id = other
    prepared = queue(seed_env, other)
    rows = seeds(seed_env)
    assert len(rows) == 2
    new = next(row for row in rows if row.id != old.id)
    assert new.advertiser_id == "primary-b"
    assert state(prepared.task_id)[0].seed_id == new.id
    assert wire[0] == []


@pytest.mark.parametrize(
    "status,changes",
    [
        ("result_unknown", {"definite_no_effect": True, "send_armed": True}),
        ("failed", {"definite_no_effect": False}),
        ("failed", {"definite_no_effect": True, "video_id": "receipt-vid"}),
    ],
)
def test_unknown_or_unsettled_seed_evidence_never_starts_another_generation(
    seed_env, wire, status, changes
):
    queue(seed_env, seed_env["target"])
    old = seeds(seed_env)[0]
    with Session(engine) as db, db.begin():
        owner = db.get(MaterialDistribution, old.distribution_id)
        operation = db.get(MaterialAssetOperation, owner.operation_id)
        owner.status = "result_unknown" if status == "result_unknown" else "blocked"
        operation.status = status
        operation.remote_response = {**operation.remote_response, **changes}
        other = target(db, seed_env, advertiser_id="target-b2")
    prepared = queue(seed_env, other)
    assert state(prepared.task_id)[0].seed_id == old.id
    assert len(seeds(seed_env)) == 1
    assert wire[0] == []


def test_delayed_primary_alias_waiter_verifies_stale_video_before_ready(
    seed_env, redis_client, wire
):
    queue(seed_env, seed_env["target"])
    owner = seeds(seed_env)[0]
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, seed_env["material_id"])
        alias = MaterialFile(
            **(original.model_dump() | {"id": uuid4(), "object_key": str(uuid4())})
        )
        db.add(alias)
        alias_env = {**seed_env, "material_id": alias.id}
    waiter = queue(alias_env, seed_env["primary"])
    complete_seed(seed_env, redis_client, wire, owner)
    with Session(engine) as db, db.begin():
        primary = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == seed_env["context"].tenant_id,
                AccountMaterial.bc_id == seed_env["bc_id"],
                AccountMaterial.advertiser_id == seed_env["primary"],
            )
        ).one()
        primary.verified_at = datetime.now(UTC) - timedelta(minutes=20)
        primary.image_id = "real-primary-cover"
    run(seed_env, redis_client, waiter.task_id, kind="prepare")
    assert state(waiter.task_id)[0].status == "verifying", (
        "Expired evidence cannot complete a waiting alias"
    )
    assert read(alias_env, seed_env["primary"]).state != "ready"
    wire[1].append(info(vid="primary-vid", material_id="primary-mid"))
    run(seed_env, redis_client, waiter.task_id)
    dist, operation, mapping = state(waiter.task_id)
    assert dist.status == "ready" and operation.status == "succeeded"
    assert (
        mapping.video_id == "primary-vid" and mapping.image_id == "real-primary-cover"
    )
    assert read(alias_env, seed_env["primary"]).state == "ready"
    assert [call[0] for call in wire[0]] == ["GET", "POST", "GET", "GET"]
