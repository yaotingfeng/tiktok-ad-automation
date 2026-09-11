"""过期授权事实通过同冻结路由的完整观察恢复；只替换物理HTTP。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, delete, select

from app.core.db import engine
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import DiscoveryRun, ExternalAssetOwner
from tests.modules.accounts.capabilities.conftest import (
    capability_env as capability_env,
)
from tests.modules.accounts.capabilities.conftest import source_env as source_env
from tests.modules.accounts.capabilities.conftest import wire as wire
from tests.modules.accounts.capabilities.test_service import run, start


@pytest.fixture
def expired_env(capability_env, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "BC_CAPABILITY_MAX_AGE_SECONDS", 14400)
    env = capability_env
    with Session(engine) as session, session.begin():
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        facts.upstream_subject = None
        facts.verified_at = datetime.now(UTC) - timedelta(hours=5)
    yield env
    with Session(engine) as session, session.begin():
        session.exec(
            delete(ExternalAssetOwner).where(
                ExternalAssetOwner.owner_tenant_id == env["context"].tenant_id
            )
        )


def test_expired_capability_queues_complete_directory_on_original_route(
    expired_env, redis_client, wire
):
    env = expired_env
    job_id = start(env)
    job = run(env, redis_client, job_id)
    with Session(engine) as session:
        discovery = session.exec(
            select(DiscoveryRun).where(
                DiscoveryRun.connection_id == env["connection_id"]
            )
        ).one_or_none()
        assert discovery is not None, "过期重检必须排同原route的完整目录观察"
        assert discovery.work["capability_job_id"] == str(job_id)
        assert discovery.work["route"]["connection_id"] == str(env["connection_id"])
        assert discovery.work["bc_id"] == env["bc_id"]
        assert discovery.work["mode"] == "RUNTIME_REFRESH"
        assert (
            not discovery.candidate_attempt_id
            and not discovery.mcp_candidate_attempt_id
        )
    assert job.status == "PENDING"
    assert wire[0] == []


def runtime_step(env, redis_client, run_id, revision=None):
    from app.modules.accounts.runtime_directory_tasks import process_runtime_directory

    with Session(engine) as session:
        row = session.get(DiscoveryRun, run_id)
        revision = row.revision if revision is None else revision
    process_runtime_directory(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=env["context"].tenant_id,
        actor_id=env["context"].actor_id,
        payload={"run_id": str(run_id), "revision": revision},
    )
    with Session(engine) as session:
        return session.get(DiscoveryRun, run_id)


def queued_run(env, redis_client):
    job_id = start(env)
    run(env, redis_client, job_id)
    with Session(engine) as session:
        row = session.exec(
            select(DiscoveryRun).where(
                DiscoveryRun.connection_id == env["connection_id"]
            )
        ).one()
        return job_id, row.id


def directory_responses(env, wire, ids=("actual-account",)):
    from tests.modules.accounts.capabilities.test_service import page

    wire[1].append({"list": [{"advertiser_id": aid} for aid in ids]})
    wire[1].append(
        {
            "list": [{"bc_info": {"bc_id": env["bc_id"], "name": "Observed BC"}}],
            "page_info": {
                "page": 1,
                "page_size": 50,
                "total_page": 1,
                "total_number": 1,
            },
        }
    )
    for n in range((len(ids) + 49) // 50):
        chunk = ids[n * 50 : (n + 1) * 50]
        wire[1].append(page(chunk, n + 1, len(ids)))
        wire[1].append(
            {
                "list": [
                    {
                        "advertiser_id": aid,
                        "name": "Observed advertiser",
                        "currency": "USD",
                        "timezone": "UTC",
                        "status": "STATUS_ENABLE",
                    }
                    for aid in chunk
                ]
            }
        )
    for n in range((len(ids) + 49) // 50):
        wire[1].append(page(ids[n * 50 : (n + 1) * 50], n + 1, len(ids)))


def finish_runtime(env, redis_client, run_id):
    for _ in range(30):
        row = runtime_step(env, redis_client, run_id)
        if row.status in {"COMPLETE", "ERROR", "CANCELLED"}:
            return row
    raise AssertionError(f"runtime did not finish: {row.work}")


def test_complete_directory_refresh_restores_expired_api_facts_and_same_job(
    expired_env, redis_client, wire
):
    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.accounts.discovery_models import DiscoveryStagedPage
    from app.modules.accounts.models import TikTokConnection
    from app.modules.accounts.routing import freeze_route, verify_route

    env = expired_env
    job_id, run_id = queued_run(env, redis_client)
    with Session(engine) as session:
        original_basis = session.get(CapabilityJob, job_id).directory_basis
        frozen = freeze_route(
            session,
            context=env["context"],
            bc_id=env["bc_id"],
            connection_id=env["connection_id"],
        )
    directory_responses(env, wire)
    finished = finish_runtime(env, redis_client, run_id)
    assert finished.status == "COMPLETE", finished.error_code
    completed = run(env, redis_client, job_id)
    assert completed.id == job_id and completed.status == "COMPLETE"
    assert completed.directory_basis != original_basis
    with Session(engine) as session:
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        connection = session.get(TikTokConnection, env["connection_id"])
        assert connection.authorization_revision == connection.credential_revision == 0
        assert facts.verified_at > datetime.now(UTC) - timedelta(minutes=1)
        assert facts.permission_summary == {
            "read_authorized": True,
            "upload_authorized": True,
            "build_authorized": True,
        }
        verify_route(
            session,
            context=env["context"],
            route=frozen,
            advertiser_id="actual-account",
            capability="build",
        )
        pages = session.exec(
            select(DiscoveryStagedPage).where(DiscoveryStagedPage.run_id == run_id)
        ).all()
        assert {p.stage for p in pages} == {
            "SUBJECT",
            "AUTHORIZED",
            "BCS",
            "ASSETS",
            "DETAILS",
            "ROLES",
        }
        assert all(len(p.rows) <= 50 for p in pages)
    assert len(wire[0]) == 5 and not wire[1]
    assert all(method == "GET" for method, _, _ in wire[0])


@pytest.mark.parametrize("scope", ["[]", "[2]", "null"])
def test_empty_or_changed_actual_scope_cannot_advance_old_fact_time(
    expired_env, redis_client, wire, scope
):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    env = expired_env
    with Session(engine) as session, session.begin():
        connection = session.get(TikTokConnection, env["connection_id"])
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=connection.tenant_id,
            value={"access_token": "synthetic-changed-material", "scope": scope},
        )
        before = (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == connection.id
                )
            )
            .one()
            .verified_at
        )
        old_grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == connection.id
            )
        ).one()
        old_checked = old_grant.checked_at
    _, run_id = queued_run(env, redis_client)
    directory_responses(env, wire)
    finished = finish_runtime(env, redis_client, run_id)
    assert finished.status == "ERROR"
    assert finished.error_code == "route_authorization_changed"
    with Session(engine) as session:
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == env["connection_id"]
            )
        ).one()
        assert facts.verified_at == before
        assert grant.checked_at == old_checked


def test_midscan_failure_preserves_old_directory_and_fact_time(
    expired_env, redis_client, wire
):
    from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess

    env = expired_env
    _, run_id = queued_run(env, redis_client)
    with Session(engine) as session:
        old = (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == env["connection_id"]
                )
            )
            .one()
            .verified_at
        )
        name = session.get(
            AdvertiserAccount, (env["context"].tenant_id, "actual-account")
        ).name
    directory_responses(env, wire)
    wire[1][3] = RuntimeError("synthetic transport failure")
    finished = finish_runtime(env, redis_client, run_id)
    assert finished.status == "ERROR"
    with Session(engine) as session:
        assert (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == env["connection_id"]
                )
            )
            .one()
            .verified_at
            == old
        )
        assert (
            session.get(
                AdvertiserAccount, (env["context"].tenant_id, "actual-account")
            ).name
            == name
        )
        grant = session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.connection_id == env["connection_id"]
            )
        ).one()
        assert grant.in_bc and grant.authorized and grant.active


def test_runtime_50_row_pages_and_old_dispatch_replay(expired_env, redis_client, wire):
    from app.modules.accounts.discovery_models import DiscoveryStagedPage

    env = expired_env
    _, run_id = queued_run(env, redis_client)
    ids = ("actual-account",) + tuple(f"new-{n}" for n in range(50))
    directory_responses(env, wire, ids)
    assert runtime_step(env, redis_client, run_id).work["stage"] == "AUTHORIZED"
    assert (
        runtime_step(env, redis_client, run_id, revision=0).work["stage"]
        == "AUTHORIZED"
    )
    assert not wire[0]
    final = finish_runtime(env, redis_client, run_id)
    assert final.status == "COMPLETE", final.error_code
    with Session(engine) as session:
        for stage in ["AUTHORIZED", "ASSETS", "DETAILS", "ROLES"]:
            pages = session.exec(
                select(DiscoveryStagedPage)
                .where(
                    DiscoveryStagedPage.run_id == run_id,
                    DiscoveryStagedPage.stage == stage,
                )
                .order_by(DiscoveryStagedPage.page)
            ).all()
            assert [len(p.rows) for p in pages] == [50, 1]
    assert len(wire[0]) == 8
    runtime_step(env, redis_client, run_id, revision=0)
    assert len(wire[0]) == 8


def test_default_change_and_credential_rotation_keep_original_runtime_route(
    expired_env, redis_client, wire
):
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TikTokConnection

    env = expired_env
    job_id, run_id = queued_run(env, redis_client)
    assert runtime_step(env, redis_client, run_id).work["stage"] == "AUTHORIZED"
    with Session(engine) as session, session.begin():
        other = TikTokConnection(tenant_id=env["context"].tenant_id, status="ACTIVE")
        session.add(other)
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=other.tenant_id,
                bc_id=env["bc_id"],
                connection_id=other.id,
                kind=other.kind,
            )
        )
        session.flush()
        default = session.get(BCDefaultRoute, (other.tenant_id, env["bc_id"]))
        assert default is not None
        default.connection_id = other.id
        connection = session.get(TikTokConnection, env["connection_id"])
        connection.credential_revision += 1
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=connection.tenant_id,
            value={"access_token": "synthetic-rotated-token", "scope": "[2,6]"},
        )
        default_id = other.id
    directory_responses(env, wire)
    assert finish_runtime(env, redis_client, run_id).status == "COMPLETE"
    assert run(env, redis_client, job_id).status == "COMPLETE"
    with Session(engine) as session:
        connection = session.get(TikTokConnection, env["connection_id"])
        assert (connection.credential_revision, connection.authorization_revision) == (
            1,
            0,
        )
        assert (
            session.get(
                BCDefaultRoute, (env["context"].tenant_id, env["bc_id"])
            ).connection_id
            == default_id
        )
    assert all(
        call[2]["headers"]["Access-Token"] == "synthetic-rotated-token"
        for call in wire[0]
    )


@pytest.mark.parametrize("change", ["role", "revision", "claim"])
def test_current_authority_or_claim_change_after_http_blocks_stage_publication(
    expired_env, redis_client, wire, change
):
    from uuid import uuid4

    from app.modules.accounts.discovery_models import DiscoveryStagedPage
    from app.modules.accounts.models import TikTokConnection
    from app.modules.tenants.models import TenantMembership

    env = expired_env
    _, run_id = queued_run(env, redis_client)
    runtime_step(env, redis_client, run_id)

    def response_after_change():
        with Session(engine) as session, session.begin():
            if change == "role":
                member = session.get(
                    TenantMembership,
                    (env["context"].tenant_id, env["context"].actor_id),
                )
                member.role = "viewer"
            elif change == "revision":
                connection = session.get(TikTokConnection, env["connection_id"])
                connection.authorization_revision += 1
            else:
                row = session.get(DiscoveryRun, run_id)
                row.claim_id = uuid4()
        return {"list": [{"advertiser_id": "actual-account"}]}

    wire[1].append(response_after_change)
    runtime_step(env, redis_client, run_id)
    with Session(engine) as session:
        assert (
            session.exec(
                select(DiscoveryStagedPage).where(
                    DiscoveryStagedPage.run_id == run_id,
                    DiscoveryStagedPage.stage == "AUTHORIZED",
                )
            ).first()
            is None
        )
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        assert facts.verified_at < datetime.now(UTC) - timedelta(hours=4)
    assert len(wire[0]) == 1


def test_removed_build_permission_prevents_first_http_and_closes_expired_claim(
    expired_env, redis_client, wire
):
    from uuid import uuid4

    from app.modules.tenants.models import TenantMembership

    env = expired_env
    _, run_id = queued_run(env, redis_client)
    with Session(engine) as session, session.begin():
        member = session.get(
            TenantMembership, (env["context"].tenant_id, env["context"].actor_id)
        )
        member.role = "viewer"
        row = session.get(DiscoveryRun, run_id)
        row.claim_id = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    final = runtime_step(env, redis_client, run_id)
    assert final.status == "ERROR" and final.error_code == "action_forbidden"
    assert wire[0] == []


def test_runtime_task_loader_and_queue_are_registered():
    from app.jobs.celery_app import celery_app
    from app.jobs.tasks import dispatch_queue

    celery_app.loader.import_default_modules()
    assert dispatch_queue("accounts.runtime_discover") == "resources"
    task = celery_app.tasks["accounts.runtime_discover"]
    assert task.time_limit == 45 and task.soft_time_limit == 40
    assert task.acks_late and task.reject_on_worker_lost


def test_completed_role_job_is_not_reused_when_authorization_facts_expire(
    capability_env, redis_client, wire, monkeypatch
):
    from uuid import uuid4

    from app.core.config import settings
    from tests.modules.accounts.capabilities.test_service import page

    env = capability_env
    monkeypatch.setattr(settings, "BC_CAPABILITY_MAX_AGE_SECONDS", 14400)
    old_job = start(env)
    wire[1].append(page(["actual-account"]))
    assert run(env, redis_client, old_job).phase == "PUBLISH"
    assert run(env, redis_client, old_job).status == "COMPLETE"
    with Session(engine) as session, session.begin():
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        facts.upstream_subject = None
        facts.verified_at = datetime.now(UTC) - timedelta(hours=5)
    new_job = start({**env, "request_id": uuid4()})
    assert new_job != old_job
    run(env, redis_client, new_job)
    with Session(engine) as session:
        assert session.exec(
            select(DiscoveryRun).where(
                DiscoveryRun.connection_id == env["connection_id"]
            )
        ).one().work["capability_job_id"] == str(new_job)


def test_recovery_reclaims_expired_claim_and_duplicate_delivery_does_not_create_run(
    expired_env, redis_client, wire
):
    from uuid import uuid4

    from app.jobs.models import PendingDispatch
    from app.modules.accounts.capability_models import CapabilityJob

    env = expired_env
    job_id, run_id = queued_run(env, redis_client)
    with Session(engine) as session, session.begin():
        row = session.get(DiscoveryRun, run_id)
        row.claim_id = uuid4()
        row.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        job = session.get(CapabilityJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
    run(env, redis_client, job_id)
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(DiscoveryRun).where(
                        DiscoveryRun.connection_id == env["connection_id"]
                    )
                ).all()
            )
            == 1
        )
    assert runtime_step(env, redis_client, run_id).work["stage"] == "AUTHORIZED"
    with Session(engine) as session:
        rows = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == env["context"].tenant_id,
                PendingDispatch.task_name == "accounts.runtime_discover",
            )
        ).all()
        assert any("recover:" in row.task_key for row in rows)
        assert all(set(row.payload) == {"run_id", "revision"} for row in rows)
    assert wire[0] == []


def test_two_workers_racing_for_one_unclaimed_page_make_one_http(
    expired_env, redis_client, wire
):
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from threading import Barrier, Event, local

    from sqlalchemy import Engine, event

    env = expired_env
    _, run_id = queued_run(env, redis_client)
    runtime_step(env, redis_client, run_id)  # SUBJECT已完成，下一页确有物理HTTP。
    rendezvous, release, state = Barrier(2), Event(), local()

    def before_sql(_conn, _cursor, statement, _params, _context, _many):
        if (
            "FROM tiktok_connection" in statement
            and "FOR UPDATE" in statement
            and not getattr(state, "waited", False)
        ):
            state.waited = True
            rendezvous.wait(timeout=3)

    def reply():
        assert release.wait(timeout=4)
        return {"list": [{"advertiser_id": "actual-account"}]}

    wire[1].extend([reply, reply])
    event.listen(Engine, "before_cursor_execute", before_sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(runtime_step, env, redis_client, run_id) for _ in range(2)
            ]
            try:
                done, _ = wait(futures, timeout=3, return_when=FIRST_COMPLETED)
                assert len(done) == 1, (
                    "第二个worker必须在第一个HTTP结束前发现已有claim并退出"
                )
            finally:
                release.set()
            [future.result(timeout=5) for future in futures]
    finally:
        release.set()
        event.remove(Engine, "before_cursor_execute", before_sql)
    assert len(wire[0]) == 1
    with Session(engine) as session:
        assert session.get(DiscoveryRun, run_id).work["stage"] == "BCS"


def test_runtime_never_binds_other_observed_bcs_or_rewrites_other_connection_grant(
    expired_env, redis_client, wire
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import BCAccountAccess, TenantBC, TikTokConnection

    env = expired_env
    with Session(engine) as session, session.begin():
        other = TikTokConnection(tenant_id=env["context"].tenant_id, status="ACTIVE")
        session.add(other)
        session.flush()
        session.add(
            BCConnectionBinding(
                tenant_id=other.tenant_id,
                bc_id=env["bc_id"],
                connection_id=other.id,
                kind=other.kind,
            )
        )
        grant = BCAccountAccess(
            tenant_id=other.tenant_id,
            bc_id=env["bc_id"],
            advertiser_id="actual-account",
            connection_id=other.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_build=True,
            can_upload=True,
            permission_state="VERIFIED",
            checked_at=datetime.now(UTC),
        )
        session.add(grant)
        other_id, checked_at = other.id, grant.checked_at
    _, run_id = queued_run(env, redis_client)
    directory_responses(env, wire)
    wire[1][1]["list"].append(
        {"bc_info": {"bc_id": "visible-unselected", "name": "Other visible BC"}}
    )
    wire[1][1]["page_info"]["total_number"] = 2
    assert finish_runtime(env, redis_client, run_id).status == "COMPLETE"
    with Session(engine) as session:
        grant = session.get(
            BCAccountAccess,
            (env["context"].tenant_id, env["bc_id"], "actual-account", other_id),
        )
        assert grant.can_build and grant.can_upload and grant.checked_at == checked_at
        assert (
            session.get(TenantBC, (env["context"].tenant_id, "visible-unselected"))
            is None
        )
        assert (
            len(
                session.exec(
                    select(BCConnectionBinding).where(
                        BCConnectionBinding.connection_id == env["connection_id"]
                    )
                ).all()
            )
            == 1
        )


def test_conflicting_new_job_basis_rolls_back_directory_and_facts_atomically(
    expired_env, redis_client, wire
):
    from uuid import uuid4

    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.accounts.runtime_directory import publish_runtime_directory

    env = expired_env
    job_id, run_id = queued_run(env, redis_client)
    directory_responses(env, wire)
    for _ in range(6):
        staged = runtime_step(env, redis_client, run_id)
    assert staged.work["stage"] == "FINALIZE"
    claim = uuid4()
    with Session(engine) as session, session.begin():
        row = session.get(DiscoveryRun, run_id)
        row.claim_id, row.claimed_until = (
            claim,
            datetime.now(UTC) + timedelta(minutes=1),
        )
        old_time = (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == env["connection_id"]
                )
            )
            .one()
            .verified_at
        )
    # 只在回滚事务中取得此次真实merge产生的新basis，随后制造数据库允许的另一活跃job。
    with Session(engine) as session:
        publish_runtime_directory(
            session,
            context=env["context"],
            run_id=run_id,
            claim_id=claim,
            revision=staged.revision,
        )
        new_basis = session.get(CapabilityJob, job_id).directory_basis
        session.rollback()
    with Session(engine) as session, session.begin():
        original = session.get(CapabilityJob, job_id)
        competing = CapabilityJob(
            tenant_id=original.tenant_id,
            bc_id=original.bc_id,
            connection_id=original.connection_id,
            actor_id=original.actor_id,
            credential_revision=original.credential_revision,
            channel=original.channel,
            authorization_revision=original.authorization_revision,
            adapter_contract_revision=original.adapter_contract_revision,
            directory_basis=new_basis,
        )
        session.add(competing)
        competing_id = competing.id
        row = session.get(DiscoveryRun, run_id)
        row.claim_id = row.claimed_until = None
    assert runtime_step(env, redis_client, run_id).status == "ERROR"
    with Session(engine) as session:
        assert session.get(CapabilityJob, job_id).status == "STALE"
        assert session.get(CapabilityJob, competing_id).status == "PENDING"
        assert (
            session.exec(
                select(ConnectionAuthorization).where(
                    ConnectionAuthorization.connection_id == env["connection_id"]
                )
            )
            .one()
            .verified_at
            == old_time
        )
        from app.modules.accounts.models import AdvertiserAccount

        assert (
            session.get(
                AdvertiserAccount, (env["context"].tenant_id, "actual-account")
            ).name
            != "Observed advertiser"
        )


def test_missing_exact_role_total_does_not_publish_fresh_authorization(
    expired_env, redis_client, wire
):
    env = expired_env
    _, run_id = queued_run(env, redis_client)
    directory_responses(env, wire)
    wire[1][-1]["page_info"].pop("total_number")
    completed = finish_runtime(env, redis_client, run_id)
    assert completed.status == "ERROR"
    with Session(engine) as session:
        facts = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == env["connection_id"]
            )
        ).one()
        assert facts.verified_at < datetime.now(UTC) - timedelta(hours=4)


def test_complete_authorized_set_restricts_other_bc_only_on_same_connection(
    expired_env, redis_client, wire
):
    from app.modules.accounts.connection_models import BCConnectionBinding
    from app.modules.accounts.models import (
        AdvertiserAccount,
        BCAccountAccess,
        TenantBC,
        TikTokConnection,
    )

    env = expired_env
    tenant_id, bc2 = env["context"].tenant_id, "synthetic-second-bc"
    checked_at = datetime.now(UTC) - timedelta(minutes=30)
    with Session(engine) as session, session.begin():
        other = TikTokConnection(tenant_id=tenant_id, status="ACTIVE")
        session.add(other)
        session.add(TenantBC(tenant_id=tenant_id, bc_id=bc2, name="Existing BC"))
        session.add(
            AdvertiserAccount(
                tenant_id=tenant_id,
                advertiser_id="withdrawn-account",
                name="Keep metadata",
                currency="USD",
                timezone="UTC",
                remote_status="STATUS_ENABLE",
            )
        )
        session.flush()
        other_id = other.id
        for connection_id in (env["connection_id"], other_id):
            session.add(
                BCConnectionBinding(
                    tenant_id=tenant_id,
                    bc_id=bc2,
                    connection_id=connection_id,
                    kind="OFFICIAL_API",
                )
            )
            session.flush()
            for aid in ("actual-account", "withdrawn-account"):
                session.add(
                    BCAccountAccess(
                        tenant_id=tenant_id,
                        bc_id=bc2,
                        advertiser_id=aid,
                        connection_id=connection_id,
                        in_bc=True,
                        authorized=True,
                        active=True,
                        can_build=True,
                        can_upload=True,
                        permission_state="VERIFIED",
                        checked_at=checked_at,
                    )
                )
    _, run_id = queued_run(env, redis_client)
    directory_responses(env, wire)
    for _ in range(6):
        staged = runtime_step(env, redis_client, run_id)
    assert staged.work["stage"] == "FINALIZE"
    with Session(engine) as session:
        assert session.get(
            BCAccountAccess, (tenant_id, bc2, "withdrawn-account", env["connection_id"])
        ).authorized, "完整观察发布前保留旧快照"
    assert finish_runtime(env, redis_client, run_id).status == "COMPLETE"
    with Session(engine) as session:
        for connection_id in (env["connection_id"], other_id):
            for aid in ("actual-account", "withdrawn-account"):
                grant = session.get(
                    BCAccountAccess, (tenant_id, bc2, aid, connection_id)
                )
                retained = connection_id == other_id or aid == "actual-account"
                assert grant.authorized is retained
                assert grant.active is retained
                assert grant.can_build is retained and grant.can_upload is retained
                assert grant.in_bc and grant.checked_at == checked_at
