"""跨 BC 首次转存复用真实任务；平台只在传输边界替身。"""

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import MaterialDistribution, MaterialFile
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import read, target
from tests.modules.materials.test_remote_only_distribution import info
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


@pytest.fixture
def seed_env(remote_env):
    env = {**remote_env, "material_id": remote_env["source_material_id"]}
    with Session(engine) as db, db.begin():
        primary = target(db, env, advertiser_id="primary-b")
        bc = db.get(TenantBC, (env["context"].tenant_id, env["bc_id"]))
        bc.material_advertiser_id = primary
    return {**env, "primary": primary}


def test_foreign_material_waits_for_primary_then_native_target(
    seed_env, redis_client, wire
):
    prepared = queue(seed_env, seed_env["target"])
    assert prepared.state == "queued"
    with Session(engine) as db:
        rows = db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == seed_env["context"].tenant_id
            )
        ).all()
        assert {r.advertiser_id for r in rows} == {"primary-b", "target-account"}
        seed_id = next(r.id for r in rows if r.advertiser_id == "primary-b")
    assert prepared.task_id != seed_id
    run(seed_env, redis_client, prepared.task_id, kind="prepare")
    assert wire[0] == []
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(seed_env, redis_client, seed_id, kind="prepare")
    wire[1].append(info(vid="primary-vid", material_id="primary-mid"))
    run(seed_env, redis_client, seed_id)
    assert state(seed_id)[0].status == "ready"
    wire[1].extend(
        [
            info(vid="primary-vid", material_id="primary-mid", file_name="primary.mp4"),
            {},
        ]
    )
    run(seed_env, redis_client, prepared.task_id, kind="prepare")
    wire[1].append(
        {
            **info(vid="target-vid", material_id="target-mid", file_name="primary.mp4"),
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": 1,
            },
        }
    )
    run(seed_env, redis_client, prepared.task_id)
    assert state(prepared.task_id)[0].status == "ready", state(prepared.task_id)[
        1
    ].remote_response
    assert state(prepared.task_id)[2].video_id == "target-vid"
    uploads = [dict(c[2]["fields"]) for c in wire[0] if "/video/ad/upload/" in c[1]]
    assert [c["advertiser_id"] for c in uploads] == ["primary-b"]
    with Session(engine) as db:
        assert (
            db.get(MaterialFile, seed_env["material_id"]).bc_id
            == seed_env["source_bc_id"]
        )


def test_preview_does_not_select_primary_or_create_work(seed_env, wire):
    with Session(engine) as db, db.begin():
        db.get(
            TenantBC, (seed_env["context"].tenant_id, seed_env["bc_id"])
        ).material_advertiser_id = None
    assert read(seed_env, seed_env["target"]).state == "preparable"
    with Session(engine) as db:
        assert (
            db.get(
                TenantBC, (seed_env["context"].tenant_id, seed_env["bc_id"])
            ).material_advertiser_id
            is None
        )
        assert (
            db.exec(
                select(MaterialDistribution).where(
                    MaterialDistribution.tenant_id == seed_env["context"].tenant_id
                )
            ).all()
            == []
        )
    assert wire[0] == []


def test_failed_or_unknown_seed_never_reserves_a_second_upload(
    seed_env, redis_client, wire
):
    from urllib3.exceptions import ReadTimeoutError

    from app.modules.materials.seed_models import MaterialBCSeed

    first = queue(seed_env, seed_env["target"])
    with Session(engine) as db:
        seed = db.exec(
            select(MaterialBCSeed).where(
                MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
            )
        ).one()
        seed_id = seed.distribution_id
    wire[1].extend(
        [info(), ReadTimeoutError(None, "https://offline.invalid", "timeout")]
    )
    run(seed_env, redis_client, seed_id, kind="prepare")
    assert state(seed_id)[0].status == "result_unknown"
    with Session(engine) as db, db.begin():
        other = target(db, seed_env, advertiser_id="target-b2")
    second = queue(seed_env, other)
    run(seed_env, redis_client, second.task_id, kind="prepare")
    run(seed_env, redis_client, seed_id, kind="prepare")
    assert state(second.task_id)[0].status == "result_unknown"
    assert state(first.task_id)[2] is None
    assert state(second.task_id)[2] is None
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(MaterialBCSeed).where(
                        MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
                    )
                ).all()
            )
            == 1
        )
    assert len([c for c in wire[0] if "/video/ad/upload/" in c[1]]) == 1


def test_primary_as_target_uses_actual_seed_target_identity(seed_env, wire):
    prepared = queue(seed_env, seed_env["primary"])
    dist, _, _ = state(prepared.task_id)
    assert dist.advertiser_id == seed_env["primary"] and dist.seed_id is None
    assert wire[0] == []


def test_preview_blocks_revoked_primary_without_selecting_another(seed_env, wire):
    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                seed_env["context"].tenant_id,
                seed_env["bc_id"],
                seed_env["primary"],
                seed_env["connection_id"],
            ),
        ).can_upload = False
    assert read(seed_env, seed_env["target"]).state == "blocked"
    assert wire[0] == []


def test_cross_bc_same_advertiser_id_remains_a_foreign_source(seed_env, wire):
    from datetime import UTC, datetime

    from app.modules.accounts.models import BCAccountAccess

    with Session(engine) as db, db.begin():
        db.add(
            BCAccountAccess(
                tenant_id=seed_env["context"].tenant_id,
                bc_id=seed_env["bc_id"],
                advertiser_id="actual-account",
                connection_id=seed_env["connection_id"],
                in_bc=True,
                authorized=True,
                active=True,
                can_build=True,
                can_upload=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            )
        )
    prepared = queue(seed_env, "actual-account")
    assert prepared.state == "queued"
    assert state(prepared.task_id)[0].advertiser_id == "actual-account"
    assert state(prepared.task_id)[0].seed_id is not None
    assert wire[0] == []


def test_verified_alias_on_same_target_reuses_video_without_share(seed_env, wire):
    from uuid import uuid4

    from app.modules.materials.models import AccountMaterial
    from tests.modules.materials.test_readiness import asset

    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, seed_env["material_id"])
        alias = MaterialFile(
            **(original.model_dump() | {"id": uuid4(), "object_key": str(uuid4())})
        )
        db.add(alias)
        db.flush()
        existing = asset(db, seed_env, seed_env["target"])
        existing.image_id = "actual-target-image"
        alias_env = {**seed_env, "material_id": alias.id}
    prepared = queue(alias_env, seed_env["target"])
    assert prepared.state == "ready"
    assert prepared.mapping.material_id == alias_env["material_id"]
    assert prepared.mapping.video_id == "vid-target-account"
    assert prepared.mapping.image_id == "actual-target-image"
    with Session(engine) as db:
        aliases = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == seed_env["context"].tenant_id,
                AccountMaterial.material_id == alias_env["material_id"],
            )
        ).all()
        assert len(aliases) == 1 and aliases[0].bc_id == seed_env["bc_id"]
    assert wire[0] == []


def test_foreign_stored_original_without_platform_source_is_blocked(
    seed_env, wire, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "S3_BUCKET", "offline-bucket")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "offline-key")
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "offline-secret")
    from sqlalchemy import delete

    from app.modules.materials.models import AccountMaterial

    with Session(engine) as db, db.begin():
        db.execute(
            delete(AccountMaterial).where(
                AccountMaterial.tenant_id == seed_env["context"].tenant_id
            )
        )
        original = db.get(MaterialFile, seed_env["material_id"])
        original.current_object_generation = None
        original.storage_state = "stored"
    assert read(seed_env, seed_env["target"]).state == "blocked"
    assert wire[0] == []


def test_source_revocation_blocks_seed_and_waiter_without_borrowing_target_authority(
    seed_env, redis_client, wire
):
    from app.modules.accounts.models import BCAccountAccess
    from app.modules.materials.seed_models import MaterialBCSeed

    prepared = queue(seed_env, seed_env["target"])
    with Session(engine) as db, db.begin():
        seed = db.exec(
            select(MaterialBCSeed).where(
                MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
            )
        ).one()
        seed_id = seed.distribution_id
        db.get(
            BCAccountAccess,
            (
                seed_env["context"].tenant_id,
                seed_env["source_bc_id"],
                "actual-account",
                seed_env["connection_id"],
            ),
        ).authorized = False
    run(seed_env, redis_client, seed_id, kind="prepare")
    run(seed_env, redis_client, prepared.task_id, kind="prepare")
    assert state(seed_id)[0].status == "blocked"
    assert state(prepared.task_id)[0].status == "blocked"
    assert state(prepared.task_id)[2] is None
    assert wire[0] == []


def test_rebound_target_cannot_replace_seed_frozen_generation(seed_env, wire):
    from app.core.errors import DomainError
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.materials.seed_models import MaterialBCSeed

    queue(seed_env, seed_env["target"])
    with Session(engine) as db, db.begin():
        binding = db.get(
            BCConnectionBinding,
            (
                seed_env["context"].tenant_id,
                seed_env["bc_id"],
                seed_env["connection_id"],
            ),
        )
        binding.revision += 1
        other = target(db, seed_env, advertiser_id="target-b2")
    with pytest.raises(DomainError, match=".*") as error:
        queue(seed_env, other)
    assert error.value.code == "frozen_route_changed"
    with Session(engine) as db:
        assert (
            len(
                db.exec(
                    select(MaterialBCSeed).where(
                        MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
                    )
                ).all()
            )
            == 1
        )
    assert wire[0] == []


def test_primary_alias_waiter_reuses_real_video_and_cover_after_seed(
    seed_env, redis_client, wire
):
    from uuid import uuid4

    from app.modules.materials.models import AccountMaterial
    from app.modules.materials.seed_models import MaterialBCSeed

    queue(seed_env, seed_env["target"])
    with Session(engine) as db, db.begin():
        original = db.get(MaterialFile, seed_env["material_id"])
        alias = MaterialFile(
            **(original.model_dump() | {"id": uuid4(), "object_key": str(uuid4())})
        )
        db.add(alias)
        alias_env = {**seed_env, "material_id": alias.id}
        seed = db.exec(
            select(MaterialBCSeed).where(
                MaterialBCSeed.tenant_id == seed_env["context"].tenant_id
            )
        ).one()
        seed_id = seed.distribution_id
    waiter = queue(alias_env, seed_env["primary"])
    assert waiter.task_id != seed_id
    wire[1].extend(
        [info(), [{"video_id": "primary-vid", "material_id": "primary-mid"}]]
    )
    run(seed_env, redis_client, seed_id, kind="prepare")
    wire[1].append(info(vid="primary-vid", material_id="primary-mid"))
    run(seed_env, redis_client, seed_id)
    with Session(engine) as db, db.begin():
        db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == seed_env["context"].tenant_id,
                AccountMaterial.bc_id == seed_env["bc_id"],
                AccountMaterial.advertiser_id == seed_env["primary"],
            )
        ).one().image_id = "actual-primary-cover"
    run(seed_env, redis_client, waiter.task_id, kind="prepare")
    dist, operation, mapping = state(waiter.task_id)
    assert dist.status == "ready" and operation.status == "succeeded"
    assert mapping.material_id == alias_env["material_id"]
    assert (
        mapping.video_id == "primary-vid" and mapping.image_id == "actual-primary-cover"
    )
    assert "upload_video_id" not in operation.remote_response
    assert len(wire[0]) == 3
