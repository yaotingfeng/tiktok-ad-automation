"""Real PostgreSQL revision triggers; every ID and actor is synthetic."""

import pytest
from sqlalchemy import delete, text
from sqlmodel import Session, SQLModel

from app.core.db import engine
from app.models import User
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import Tenant
from tests.modules.conftest import create_context


@pytest.fixture
def directory_env():
    with Session(engine) as session, session.begin():
        context = create_context(session)
        bc = TenantBC(tenant_id=context.tenant_id, bc_id="revision-bc")
        connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        account = AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id="revision-account",
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
        session.add_all([bc, connection, account])
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                advertiser_id=account.advertiser_id,
                connection_id=connection.id,
                in_bc=True,
                active=True,
                authorized=True,
            )
        )
        env = {
            "context": context,
            "tenant": context.tenant_id,
            "bc": bc.bc_id,
            "connection": connection.id,
            "advertiser": account.advertiser_id,
        }
    yield env
    with Session(engine) as session, session.begin():
        for table in reversed(SQLModel.metadata.sorted_tables):
            if "tenant_id" in table.c:
                session.execute(
                    delete(table).where(table.c.tenant_id == context.tenant_id)
                )
        session.execute(delete(Tenant).where(Tenant.id == context.tenant_id))
        session.execute(delete(User).where(User.id == context.actor_id))


def revision(session, env):
    return session.execute(
        text(
            "SELECT revision FROM account_directory_revision WHERE tenant_id=:tenant AND bc_id=:bc AND connection_id=:connection"
        ),
        env,
    ).scalar_one_or_none()


def test_direct_grant_mutation_changes_revision_but_flags_do_not(directory_env):
    env = directory_env
    with Session(engine) as session, session.begin():
        before = revision(session, env)
        assert before is not None
        session.execute(
            text(
                "UPDATE bc_account_access SET can_build=true,can_upload=true,permission_state='VERIFIED',checked_at=now() WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before
        session.execute(
            text(
                "UPDATE bc_account_access SET authorized=false WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 1


def test_metadata_and_new_membership_serialize_before_publishing(directory_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    env = directory_env
    with Session(engine) as session, session.begin():
        connection = TikTokConnection(tenant_id=env["tenant"], status="ACTIVE")
        session.add(connection)
        session.flush()
        new_env = {**env, "connection": connection.id}
    started, finished = Event(), Event()

    def insert():
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            started.set()
            session.add(
                BCAccountAccess(
                    tenant_id=env["tenant"],
                    bc_id=env["bc"],
                    advertiser_id=env["advertiser"],
                    connection_id=new_env["connection"],
                    authorized=True,
                    in_bc=True,
                    active=True,
                )
            )
            session.flush()
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
                ),
                env,
            )
            future = pool.submit(insert)
            assert started.wait(2)
            # The uncommitted metadata must prevent publishing membership with an
            # already-captured revision and the old visible account metadata.
            blocked = not finished.wait(0.25)
        future.result(timeout=5)
        assert blocked


def test_new_membership_then_metadata_fences_the_new_scope(directory_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    env = directory_env
    with Session(engine) as session, session.begin():
        connection = TikTokConnection(tenant_id=env["tenant"], status="ACTIVE")
        session.add(connection)
        session.flush()
        new_env = {**env, "connection": connection.id}
    started, finished = Event(), Event()

    def metadata():
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            started.set()
            session.execute(
                text(
                    "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
                ),
                env,
            )
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as session, session.begin():
            session.add(
                BCAccountAccess(
                    tenant_id=env["tenant"],
                    bc_id=env["bc"],
                    advertiser_id=env["advertiser"],
                    connection_id=new_env["connection"],
                    authorized=True,
                    in_bc=True,
                    active=True,
                )
            )
            session.flush()
            before = revision(session, new_env)
            future = pool.submit(metadata)
            assert started.wait(2)
            blocked = not finished.wait(0.25)
        future.result(timeout=5)
        assert blocked
    with Session(engine) as session:
        assert revision(session, new_env) == before + 1


@pytest.mark.parametrize(
    "assignment",
    [
        "currency='EUR'",
        "timezone='Asia/Singapore'",
        "remote_status='DISABLE'",
        "ownership_conflict=true",
    ],
)
def test_each_account_metadata_fact_invalidates(directory_env, assignment):
    env = directory_env
    with Session(engine) as session, session.begin():
        before = revision(session, env)
        session.execute(
            text(f"UPDATE advertiser_account SET {assignment} WHERE tenant_id=:tenant"),
            env,
        )
        assert revision(session, env) == before + 1
        session.execute(
            text(f"UPDATE advertiser_account SET {assignment} WHERE tenant_id=:tenant"),
            env,
        )
        assert revision(session, env) == before + 1
        session.execute(
            text(
                "UPDATE advertiser_account SET name='new label' WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 1


@pytest.mark.parametrize(
    "assignment", ["in_bc=false", "authorized=false", "active=false"]
)
def test_each_grant_fact_invalidates(directory_env, assignment):
    env = directory_env
    with Session(engine) as session, session.begin():
        before = revision(session, env)
        session.execute(
            text(f"UPDATE bc_account_access SET {assignment} WHERE tenant_id=:tenant"),
            env,
        )
        assert revision(session, env) == before + 1
        session.execute(
            text(f"UPDATE bc_account_access SET {assignment} WHERE tenant_id=:tenant"),
            env,
        )
        assert revision(session, env) == before + 1


def test_bulk_statement_deduplicates_and_rollback_restores(directory_env):
    env = directory_env
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "INSERT INTO advertiser_account (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict) SELECT :tenant,'bulk-'||i,'','USD','UTC','ENABLE',false FROM generate_series(1,205) AS i"
            ),
            env,
        )
        before = revision(session, env)
        session.execute(
            text(
                "INSERT INTO bc_account_access (tenant_id,bc_id,connection_id,advertiser_id,in_bc,authorized,active,can_upload,can_build,permission_state) SELECT :tenant,:bc,:connection,'bulk-'||i,true,true,true,false,false,'UNKNOWN' FROM generate_series(1,205) AS i"
            ),
            env,
        )
        assert revision(session, env) == before + 1
        session.execute(
            text(
                "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 2
        session.execute(
            text("UPDATE bc_account_access SET active=false WHERE tenant_id=:tenant"),
            env,
        )
        assert revision(session, env) == before + 3
    with Session(engine) as session:
        before = revision(session, env)
        session.execute(
            text(
                "UPDATE advertiser_account SET currency='JPY' WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 1
        session.rollback()
        assert revision(session, env) == before


def test_tombstone_prevents_last_grant_delete_reinsert_aba_and_parent_cascades(
    directory_env,
):
    env = directory_env
    with Session(engine) as session, session.begin():
        before = revision(session, env)
        session.execute(
            text("DELETE FROM bc_account_access WHERE tenant_id=:tenant"), env
        )
        assert revision(session, env) == before + 1
        session.add(
            BCAccountAccess(
                tenant_id=env["tenant"],
                bc_id=env["bc"],
                advertiser_id=env["advertiser"],
                connection_id=env["connection"],
                authorized=True,
                in_bc=True,
                active=True,
            )
        )
        session.flush()
        assert revision(session, env) == before + 2
        session.execute(
            text("DELETE FROM bc_account_access WHERE tenant_id=:tenant"), env
        )
        assert revision(session, env) == before + 3
        session.execute(
            text("DELETE FROM tiktok_connection WHERE tenant_id=:tenant"), env
        )
        assert revision(session, env) is None


def test_scope_move_invalidates_both_and_metadata_reaches_current_scopes(directory_env):
    env = directory_env
    with Session(engine) as session, session.begin():
        session.add(TenantBC(tenant_id=env["tenant"], bc_id="other-bc"))
        session.flush()
        other = {**env, "bc": "other-bc"}
        before = revision(session, env)
        session.execute(
            text(
                "UPDATE bc_account_access SET bc_id='other-bc' WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 1
        assert revision(session, other) == 1
        session.execute(
            text(
                "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
            ),
            env,
        )
        assert revision(session, env) == before + 1
        assert revision(session, other) == 2


def test_concurrent_distinct_account_updates_do_not_lose_increments(directory_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    env = directory_env
    with Session(engine) as session, session.begin():
        session.add(
            AdvertiserAccount(
                tenant_id=env["tenant"], advertiser_id="second", currency="USD"
            )
        )
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=env["tenant"],
                bc_id=env["bc"],
                advertiser_id="second",
                connection_id=env["connection"],
            )
        )
        session.flush()
        before = revision(session, env)
    barrier = Barrier(2)

    def mutate(advertiser):
        with Session(engine) as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            barrier.wait(timeout=2)
            session.execute(
                text(
                    "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant AND advertiser_id=:advertiser"
                ),
                {**env, "advertiser": advertiser},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(mutate, value) for value in [env["advertiser"], "second"]
        ]
        for future in futures:
            future.result(timeout=7)
    with Session(engine) as session:
        assert revision(session, env) == before + 2


def test_last_seen_run_is_directory_fact(directory_env):
    from app.modules.accounts.models import DiscoveryRun

    env = directory_env
    with Session(engine) as session, session.begin():
        run = DiscoveryRun(
            tenant_id=env["tenant"],
            actor_id=env["context"].actor_id,
            connection_id=env["connection"],
        )
        session.add(run)
        session.flush()
        before = revision(session, env)
        session.execute(
            text(
                "UPDATE bc_account_access SET last_seen_run_id=:run WHERE tenant_id=:tenant"
            ),
            {**env, "run": run.id},
        )
        assert revision(session, env) == before + 1


def test_composite_fks_reject_another_tenants_connection(directory_env):
    from sqlalchemy.exc import IntegrityError

    env = directory_env
    with Session(engine) as session:
        other = create_context(session)
        connection = TikTokConnection(tenant_id=other.tenant_id)
        session.add(connection)
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO account_directory_revision (tenant_id,bc_id,connection_id,revision) VALUES (:tenant,:bc,:foreign_connection,1)"
                ),
                {**env, "foreign_connection": connection.id},
            )
        # Roll back the complete synthetic other tenant too.
        session.rollback()


def test_truncate_keeps_tombstone_and_is_transactional(directory_env):
    env = directory_env
    with Session(engine) as session:
        before = revision(session, env)
        session.execute(text("TRUNCATE bc_account_access CASCADE"))
        assert revision(session, env) == before + 1
        session.rollback()
        assert revision(session, env) == before


def test_migration_backfills_live_scope_and_roundtrips(directory_env):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[4]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app/alembic"))
    command.downgrade(config, "0006a_unit_dispatch")
    try:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "UPDATE advertiser_account SET currency='EUR' WHERE tenant_id=:tenant"
                ),
                directory_env,
            )
    finally:
        command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        assert revision(session, directory_env) == 1
        session.execute(
            text(
                "UPDATE advertiser_account SET currency='JPY' WHERE tenant_id=:tenant"
            ),
            directory_env,
        )
        assert revision(session, directory_env) == 2
