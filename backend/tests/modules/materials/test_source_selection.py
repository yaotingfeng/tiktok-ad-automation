"""真实 PostgreSQL：集中来源、不同文件并发、已有发送身份不变。"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.accounts.routing import freeze_route
from app.modules.materials.ingest_models import (
    IngestSession,
    IngestSessionFile,
    SourceAccountLoad,
)
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.modules.materials.test_source_uploads import (  # noqa: F401
    CONTENT,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)


@pytest.fixture
def env(source_env):
    return source_env


def add_accounts(db, env, count):
    for index in range(count):
        advertiser_id = f"source-{index:03}"
        db.add(
            AdvertiserAccount(
                tenant_id=env["context"].tenant_id,
                advertiser_id=advertiser_id,
                currency="USD",
                timezone="UTC",
                remote_status="ENABLE",
            )
        )
        db.flush()
        db.add(
            BCAccountAccess(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                advertiser_id=advertiser_id,
                connection_id=env["connection_id"],
                in_bc=True,
                authorized=True,
                active=True,
                can_upload=True,
                can_build=True,
                permission_state="VERIFIED",
                checked_at=datetime.now(UTC),
            )
        )
    old = db.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == env["context"].tenant_id,
            BCAccountAccess.advertiser_id == "actual-account",
        )
    ).one()
    old.can_upload = False
    db.flush()


def add_files(db, env, count):
    parent = IngestSession(
        tenant_id=env["context"].tenant_id,
        bc_id=env["bc_id"],
        actor_id=env["context"].actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        frozen_route=freeze_route(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        ).model_dump(mode="json"),
        expected_files=count,
        expected_bytes=count * len(CONTENT),
    )
    db.add(parent)
    db.flush()
    ids = []
    for index in range(count):
        file = MaterialFile(
            tenant_id=parent.tenant_id,
            bc_id=parent.bc_id,
            file_name="Moon.mp4",
            object_key=f"test/{uuid4()}",
            byte_size=len(CONTENT),
        )
        db.add(file)
        db.flush()
        db.add(
            IngestSessionFile(
                tenant_id=parent.tenant_id,
                bc_id=parent.bc_id,
                session_id=parent.id,
                client_index=index,
                material_id=file.id,
                byte_size=file.byte_size,
                manifest_digest="a" * 64,
            )
        )
        ids.append(file.id)
    db.flush()
    return ids


def claim(db, env, material_id):
    from app.modules.materials.source_selection import claim_source_account

    return claim_source_account(
        db,
        context=env["context"],
        bc_id=env["bc_id"],
        material_id=material_id,
        route=freeze_route(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        ),
    )


def test_concurrent_accounts_are_charged_once_across_files(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with Session(engine) as db, db.begin():
        add_accounts(db, env, 1)
        ids = add_files(db, env, 2)
    barrier = Barrier(2)

    def worker(identity):
        with Session(engine) as db, db.begin():
            barrier.wait(timeout=10)
            try:
                return claim(db, env, identity).advertiser_id
            except DomainError as error:
                assert error.code == "source_capacity_pending"
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ids))
    assert results == ["source-000", "source-000"]
    with Session(engine) as db:
        assert (
            db.exec(
                select(SourceAccountLoad.in_flight).where(
                    SourceAccountLoad.tenant_id == env["context"].tenant_id
                )
            ).one()
            == 2
        )


def release(db, env, material_id, access):
    from app.modules.materials.source_selection import release_source_account

    operation = MaterialAssetOperation(
        tenant_id=env["context"].tenant_id,
        bc_id=env["bc_id"],
        material_id=material_id,
        advertiser_id=access.advertiser_id,
        path="upload_original",
        status="succeeded",
        request_digest="a" * 64,
        remote_response={
            "source_slot_held": True,
            "connection_id": str(access.connection_id),
        },
    )
    db.add(operation)
    db.flush()
    assert release_source_account(db, context=env["context"], operation=operation)
    assert not release_source_account(db, context=env["context"], operation=operation)


def test_claim_is_persisted_once_and_different_files_share_primary(env):
    with Session(engine) as db, db.begin():
        add_accounts(db, env, 2)
        ids = add_files(db, env, 3)
        first = claim(db, env, ids[0])
        same = claim(db, env, ids[0])
        second = claim(db, env, ids[1])
        assert first == same
        assert first.advertiser_id == second.advertiser_id == "source-000"
        assert claim(db, env, ids[2]).advertiser_id == "source-000"
        assert (
            sum(
                db.exec(
                    select(SourceAccountLoad.in_flight).where(
                        SourceAccountLoad.tenant_id == env["context"].tenant_id
                    )
                ).all()
            )
            == 3
        )
        row = db.exec(
            select(IngestSessionFile).where(IngestSessionFile.material_id == ids[0])
        ).one()
        assert (row.source_advertiser_id, row.connection_id) == (
            first.advertiser_id,
            first.connection_id,
        )


def test_twenty_sources_and_two_thousand_jobs_remain_concentrated(env):
    with Session(engine) as db, db.begin():
        add_accounts(db, env, 20)
        ids = add_files(db, env, 2000)
        counts = Counter()
        for offset in range(0, 2000, 20):
            assignments = [
                (identity, claim(db, env, identity))
                for identity in ids[offset : offset + 20]
            ]
            assert {access.advertiser_id for _, access in assignments} == {"source-000"}
            for identity, access in assignments:
                counts[access.advertiser_id] += 1
                release(db, env, identity, access)
        assert counts == {"source-000": 2000}
        assert (
            sum(
                db.exec(
                    select(SourceAccountLoad.in_flight).where(
                        SourceAccountLoad.tenant_id == env["context"].tenant_id
                    )
                ).all()
            )
            == 0
        )


def test_cooling_or_revoked_sources_do_not_starve_other_accounts(env):
    with Session(engine) as db, db.begin():
        add_accounts(db, env, 3)
        ids = add_files(db, env, 2)
        db.add(
            SourceAccountLoad(
                tenant_id=env["context"].tenant_id,
                bc_id=env["bc_id"],
                advertiser_id="source-000",
                connection_id=env["connection_id"],
                cooldown_until=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        grant = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id,
                BCAccountAccess.advertiser_id == "source-001",
            )
        ).one()
        grant.authorized = False
        db.flush()
        assert claim(db, env, ids[0]).advertiser_id == "source-002"
        assert claim(db, env, ids[1]).advertiser_id == "source-002"


def test_explicit_source_never_falls_back_to_another_account(env):
    from app.modules.materials.source_selection import claim_source_account

    with Session(engine) as db, db.begin():
        add_accounts(db, env, 2)
        ids = add_files(db, env, 2)
        route = freeze_route(
            db,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        )

        def choose(material_id, account):
            return claim_source_account(
                db,
                context=env["context"],
                bc_id=env["bc_id"],
                material_id=material_id,
                route=route,
                advertiser_id=account,
            )

        assert choose(ids[0], "source-001").advertiser_id == "source-001"
        assert choose(ids[1], "source-001").advertiser_id == "source-001"
        with pytest.raises(DomainError) as error:
            choose(ids[1], "outside-bc")
        assert error.value.code == "account_not_in_bc"


def test_primary_stays_fixed_during_cooldown_and_new_account_discovery(env):
    from app.modules.accounts.models import TenantBC

    with Session(engine) as db, db.begin():
        add_accounts(db, env, 2)
        ids = add_files(db, env, 3)
        first = claim(db, env, ids[0])
        bc = db.get(TenantBC, (env["context"].tenant_id, env["bc_id"]))
        assert bc.material_advertiser_id == first.advertiser_id == "source-000"
        # 一个排序更靠前的新账户不能改变已经选好的集中来源。
        old = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id,
                BCAccountAccess.advertiser_id == "actual-account",
            )
        ).one()
        old.can_upload = True
        assert claim(db, env, ids[1]).advertiser_id == "source-000"
        load = db.get(
            SourceAccountLoad,
            (
                env["context"].tenant_id,
                env["bc_id"],
                "source-000",
                env["connection_id"],
            ),
        )
        load.cooldown_until = datetime.now(UTC) + timedelta(minutes=1)
        db.flush()
        with pytest.raises(DomainError, match="冷却"):
            claim(db, env, ids[2])
        assert bc.material_advertiser_id == "source-000"
        load.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        assert claim(db, env, ids[2]).advertiser_id == "source-000"


def test_revoked_primary_never_silently_uploads_to_another_account(env):
    with Session(engine) as db, db.begin():
        add_accounts(db, env, 2)
        ids = add_files(db, env, 2)
        assert claim(db, env, ids[0]).advertiser_id == "source-000"
        grant = db.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == env["context"].tenant_id,
                BCAccountAccess.advertiser_id == "source-000",
            )
        ).one()
        grant.can_upload = False
        db.flush()
        with pytest.raises(DomainError):
            claim(db, env, ids[1])
        row = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == ids[1],
            )
        ).one()
        assert row.source_advertiser_id is None
