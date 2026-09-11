import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select
from urllib3.response import HTTPResponse

from app.core.config import settings
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.admission import admission_keys
from app.jobs.models import DispatchTenantCursor, PendingDispatch
from app.models import User
from app.modules.accounts import tasks
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    ConnectionAuthorization,
)
from app.modules.accounts.discovery import claim_external_asset, finalize_directory
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.models import (
    AdvertiserAccount,
    AuthorizationAttempt,
    BCAccountAccess,
    DiscoveryRun,
    DiscoverySeen,
    ExternalAssetOwner,
    TenantBC,
    TikTokConnection,
)
from app.modules.tenants.models import Tenant, TenantMembership
from tests.modules.accounts.test_discovery import seed_complete
from tests.modules.conftest import create_context


@pytest.fixture
def committed_runs(policy, monkeypatch):
    """Real committed rows for independent worker transactions and PostgreSQL locks."""
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    contexts = []
    runs = []
    with Session(engine) as session, session.begin():
        for _ in range(2):
            context = create_context(session, role="tenant_admin")
            contexts.append(context)
            connection = TikTokConnection(
                tenant_id=context.tenant_id,
                status="ACTIVE",
                credential_ciphertext=encrypt_credentials(
                    tenant_id=context.tenant_id, value={"access_token": "old-token"}
                ),
            )
            session.add(connection)
            session.flush()
            attempt = AuthorizationAttempt(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=connection.id,
                state_hash=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                status="CANDIDATE_READY",
                candidate_ciphertext=encrypt_credentials(
                    tenant_id=context.tenant_id,
                    value={"access_token": "old-token", "scope": "[2,6]"},
                ),
            )
            session.add(attempt)
            session.flush()
            run = DiscoveryRun(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                connection_id=connection.id,
                candidate_attempt_id=attempt.id,
                work={"stage": "SUBJECT", "page": 1},
            )
            session.add(run)
            session.flush()
            runs.append(run.id)
    try:
        yield contexts, runs
    finally:
        with Session(engine) as session, session.begin():
            tenant_ids = [c.tenant_id for c in contexts]
            session.exec(
                delete(BCAccountAccess).where(BCAccountAccess.tenant_id.in_(tenant_ids))
            )
            for model in (
                DiscoveryStagedPage,
                BCConnectionBinding,
                ConnectionAuthorization,
            ):
                session.exec(delete(model).where(model.tenant_id.in_(tenant_ids)))
            session.exec(delete(DiscoverySeen).where(DiscoverySeen.run_id.in_(runs)))
            session.exec(
                delete(DiscoveryRun).where(DiscoveryRun.tenant_id.in_(tenant_ids))
            )
            for model in (
                AuthorizationAttempt,
                TikTokConnection,
                AdvertiserAccount,
                TenantBC,
                TenantMembership,
                PendingDispatch,
                DispatchTenantCursor,
            ):
                session.exec(delete(model).where(model.tenant_id.in_(tenant_ids)))
            session.exec(
                delete(ExternalAssetOwner).where(
                    ExternalAssetOwner.owner_tenant_id.in_(tenant_ids)
                )
            )
            session.exec(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
            session.exec(
                delete(User).where(User.id.in_([c.actor_id for c in contexts]))
            )


@pytest.fixture
def directory_transport(monkeypatch, sdk_transport):  # noqa: ARG001
    calls = []
    fail = {}
    bc = f"bc-{uuid4()}"
    advertiser = f"account-{uuid4()}"

    def request(_pool, _method, url, **kwargs):
        fields = dict(kwargs["fields"])
        calls.append((url, fields, kwargs["headers"]["Access-Token"]))
        if "callback" in fail:
            fail["callback"](url, fields)
        if fail.get("path") and url.endswith(fail["path"]):
            raise RuntimeError("raw-token-query-secret")
        if url.endswith("/oauth2/advertiser/get/"):
            data = {"list": [{"advertiser_id": advertiser}]}
        elif url.endswith("/bc/get/"):
            data = {
                "list": [{"bc_info": {"bc_id": bc, "name": "BC"}}],
                "page_info": {
                    "page": 1,
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        elif url.endswith("/bc/asset/get/"):
            data = {
                "list": [
                    {
                        "asset_id": advertiser,
                        "asset_name": "Account",
                        "asset_type": "ADVERTISER",
                        "advertiser_role": "OPERATOR",
                    }
                ],
                "page_info": {
                    "page": fields["page"],
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
        else:
            data = {
                "list": [
                    {
                        "advertiser_id": advertiser,
                        "name": "Account",
                        "currency": "USD",
                        "timezone": "UTC",
                        "status": "STATUS_ENABLE",
                        "can_build": True,
                    }
                ]
            }
        if fail.get("missing_paging") and "page_info" in data:
            data.pop("page_info")
        return HTTPResponse(
            body=json.dumps({"code": 0, "request_id": "r", "data": data}).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    return calls, fail, bc, advertiser


def step(context, run_id, redis_client, revision=None):
    with Session(engine) as session:
        run = session.get(DiscoveryRun, run_id)
        initial_subject = run.work.get("stage") == "SUBJECT" and revision is None
        payload = {
            "run_id": str(run_id),
            "revision": run.revision if revision is None else revision,
        }
    tasks.process_discovery(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )

    if initial_subject:
        with Session(engine) as session:
            run = session.get(DiscoveryRun, run_id)
            proceed = run.status == "RUNNING" and run.work["stage"] == "AUTHORIZED"
        if proceed:
            step(context, run_id, redis_client)


def test_full_discovery_and_duplicate_delivery(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, bc, advertiser = directory_transport
    for _ in range(6):
        step(contexts[0], runs[0], redis_client)
    assert len(calls) == 5
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "COMPLETE" and run.sent_count == 5
        assert (
            len(
                session.exec(
                    select(DiscoveryStagedPage).where(
                        DiscoveryStagedPage.run_id == run.id
                    )
                ).all()
            )
            == 6
        )
        access = session.get(
            BCAccountAccess, (run.tenant_id, bc, advertiser, run.connection_id)
        )
        assert (
            access.active
            and access.permission_state == "UNKNOWN"
            and not access.can_build
        )
    step(contexts[0], runs[0], redis_client, revision=0)
    assert len(calls) == 5


def test_candidate_only_promoted_after_full_scan(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        attempt = AuthorizationAttempt(
            tenant_id=run.tenant_id,
            actor_id=run.actor_id,
            connection_id=run.connection_id,
            state_hash=uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            status="CANDIDATE_READY",
            candidate_ciphertext=encrypt_credentials(
                tenant_id=run.tenant_id, value={"access_token": "candidate-token"}
            ),
        )
        session.add(attempt)
        session.flush()
        run.candidate_attempt_id = attempt.id
        attempt_id = attempt.id
    for _ in range(5):
        step(contexts[0], runs[0], redis_client)
        with Session(engine) as session:
            run = session.get(DiscoveryRun, runs[0])
            conn = session.get(TikTokConnection, run.connection_id)
            assert conn.credential_revision == 0
            assert (
                decrypt_credentials(
                    tenant_id=run.tenant_id, ciphertext=conn.credential_ciphertext
                )["access_token"]
                == "old-token"
            )
    step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        conn = session.get(TikTokConnection, run.connection_id)
        assert conn.credential_revision == 1
        assert session.get(AuthorizationAttempt, attempt_id).status == "ACCEPTED"
        assert (
            decrypt_credentials(
                tenant_id=run.tenant_id, ciphertext=conn.credential_ciphertext
            )["access_token"]
            == "candidate-token"
        )
    assert {call[2] for call in calls} == {"candidate-token"}


@pytest.mark.parametrize("target", range(5))
def test_each_call_admission_defer_keeps_same_page_without_send(
    committed_runs, directory_transport, redis_client, target
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    for _ in range(target):
        step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        before = session.get(DiscoveryRun, runs[0])
        work = before.work.copy()
        if work["stage"] == "SUBJECT":
            work = {"stage": "AUTHORIZED", "page": 1}
        sent = before.sent_count
        endpoint = {
            "SUBJECT": "accounts.list_authorized_advertisers",
            "AUTHORIZED": "accounts.list_authorized_advertisers",
            "BCS": "accounts.list_bcs",
            "ASSETS": "accounts.list_bc_assets",
            "DETAILS": "accounts.get_advertisers",
            "ROLES": "accounts.list_bc_assets",
        }[work["stage"]]
    keys = admission_keys(settings.TIKTOK_APP_ID, endpoint, contexts[0].tenant_id, "")
    redis_client.zadd(
        keys[0],
        {f"block-{i}": int(datetime.now(UTC).timestamp() * 1000) for i in range(100)},
    )
    try:
        step(contexts[0], runs[0], redis_client)
        with Session(engine) as session, session.begin():
            run = session.get(DiscoveryRun, runs[0])
            assert (
                run.status == "ADMISSION_WAIT"
                and run.work == work
                and run.sent_count == sent
            )
            assert run.next_attempt_at > datetime.now(UTC)
            run.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        assert len(calls) == target
    finally:
        redis_client.delete(keys[0])
    step(contexts[0], runs[0], redis_client)
    assert len(calls) == target + 1
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status in {"RUNNING", "COMPLETE"} and run.sent_count == sent + 1


def test_missing_page_evidence_retains_progress_and_does_not_finalize(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, fail, _, _ = directory_transport
    step(contexts[0], runs[0], redis_client)
    fail["missing_paging"] = True
    step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "ERROR" and run.error_code == "unsupported_account_schema"
        assert run.work["stage"] == "BCS" and run.work["page"] == 1
        assert not session.exec(
            select(DiscoverySeen).where(DiscoverySeen.run_id == run.id)
        ).all()
    assert len(calls) == 2


def test_worker_sdk_network_holds_no_run_or_connection_locks(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    _, fail, _, _ = directory_transport
    checked = []

    def unlocked(_url, _fields):
        with Session(engine) as session, session.begin():
            run = session.exec(
                select(DiscoveryRun)
                .where(DiscoveryRun.id == runs[0])
                .with_for_update(nowait=True)
            ).one()
            session.exec(
                select(TikTokConnection)
                .where(TikTokConnection.id == run.connection_id)
                .with_for_update(nowait=True)
            ).one()
            checked.append(True)

    fail["callback"] = unlocked
    step(contexts[0], runs[0], redis_client)
    assert checked == [True]


def test_same_run_concurrent_workers_send_once(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, fail, _, _ = directory_transport
    entered, release = Event(), Event()

    def blocking(_url, _fields):
        entered.set()
        assert release.wait(5)

    fail["callback"] = blocking
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(step, contexts[0], runs[0], redis_client)
        assert entered.wait(5)
        step(contexts[0], runs[0], redis_client)
        assert len(calls) == 1
        release.set()
        future.result(timeout=5)


def test_two_tenant_asset_claim_is_serialized_and_conflict_retained(committed_runs):
    contexts, runs = committed_runs
    asset = f"shared-{uuid4()}"
    waiting = Event()

    def claim_second():
        with Session(engine) as session, session.begin():
            waiting.set()
            return claim_external_asset(
                session,
                kind="ADVERTISER",
                external_id=asset,
                tenant_id=contexts[1].tenant_id,
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as first, first.begin():
            assert claim_external_asset(
                first,
                kind="ADVERTISER",
                external_id=asset,
                tenant_id=contexts[0].tenant_id,
            )
            future = executor.submit(claim_second)
            assert waiting.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        assert future.result(timeout=5) is False
    for index, (context, run_id) in enumerate(zip(contexts, runs, strict=True)):
        with Session(engine) as session:
            run = session.get(DiscoveryRun, run_id)
            bc = f"bc-{context.tenant_id}"
            seed_complete(
                session,
                run,
                bc,
                [
                    {
                        "advertiser_id": asset,
                        "currency": "USD",
                        "timezone": "UTC",
                        "remote_status": "STATUS_ENABLE",
                    }
                ],
            )
            if index == 0:
                finalize_directory(session, run_id=run_id)
                session.commit()
            else:
                with pytest.raises(DomainError) as failure:
                    finalize_directory(session, run_id=run_id)
                assert failure.value.code == "account_ownership_conflict"
                session.rollback()
    with Session(engine) as session:
        assert session.get(AdvertiserAccount, (contexts[0].tenant_id, asset))
        assert session.get(AdvertiserAccount, (contexts[1].tenant_id, asset)) is None
        assert not session.exec(
            select(BCAccountAccess).where(
                BCAccountAccess.tenant_id == contexts[1].tenant_id
            )
        ).all()


def test_database_enforces_one_active_generation_and_tenant_fks(committed_runs):
    contexts, runs = committed_runs
    with Session(engine) as session:
        first = session.get(DiscoveryRun, runs[0])
        second = session.get(DiscoveryRun, runs[1])
        connection_id = first.connection_id
        other_connection_id = second.connection_id
    for target in (connection_id, other_connection_id):
        with Session(engine) as session:
            session.add(
                DiscoveryRun(
                    tenant_id=contexts[0].tenant_id,
                    actor_id=contexts[0].actor_id,
                    connection_id=target,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_production_handler_rejects_unbounded_execution():
    assert tasks.discover.time_limit == 45 and tasks.discover.soft_time_limit == 40
    with pytest.raises(DomainError) as error:
        tasks.discover(tenant_id=str(uuid4()), actor_id=str(uuid4()), payload={})
    assert error.value.code == "discovery_worker_unbounded"


def test_second_page_failure_resumes_same_run_and_does_not_remove_old_access(
    committed_runs, directory_transport, redis_client, monkeypatch
):
    contexts, runs = committed_runs
    _, _, bc, advertiser = directory_transport
    # Canonical SDK-envelope transport; two BCs have independent two-page asset catalogs.
    calls = []
    fail = {"enabled": True}

    def request(_pool, _method, url, **kwargs):
        fields = dict(kwargs["fields"])
        calls.append((url, fields))
        if url.endswith("/oauth2/advertiser/get/"):
            data = {"list": [{"advertiser_id": advertiser}]}
        elif url.endswith("/bc/get/"):
            page = fields["page"]
            data = {
                "list": [
                    {"bc_info": {"bc_id": f"{bc}-{n}", "name": "BC"}} for n in (1, 2)
                ],
                "page_info": {
                    "page": page,
                    "page_size": 50,
                    "total_page": 1,
                    "total_number": 2,
                },
            }
        elif url.endswith("/bc/asset/get/"):
            page = fields["page"]
            if page == 2 and fail["enabled"]:
                raise RuntimeError("raw-secret")
            data = {
                "list": [
                    {
                        "asset_id": identity,
                        "asset_name": "A",
                        "asset_type": "ADVERTISER",
                        "advertiser_role": "ADMIN",
                    }
                    for identity in (
                        [advertiser] + [f"extra-{i}" for i in range(49)]
                        if page == 1
                        else ["last-extra"]
                    )
                ],
                "page_info": {
                    "page": page,
                    "page_size": 50,
                    "total_page": 2,
                    "total_number": 51,
                },
            }
        else:
            data = {
                "list": [
                    {
                        "advertiser_id": advertiser,
                        "currency": "USD",
                        "timezone": "UTC",
                        "status": "STATUS_ENABLE",
                    }
                ]
            }
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": data}).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        session.add(TenantBC(tenant_id=run.tenant_id, bc_id=f"{bc}-1"))
        session.add(
            AdvertiserAccount(
                tenant_id=run.tenant_id,
                advertiser_id="old",
                currency="USD",
                timezone="UTC",
            )
        )
        session.flush()
        session.add(
            BCAccountAccess(
                tenant_id=run.tenant_id,
                bc_id=f"{bc}-1",
                advertiser_id="old",
                connection_id=run.connection_id,
                in_bc=True,
                active=True,
                authorized=True,
            )
        )
    for _ in range(6):
        step(contexts[0], runs[0], redis_client)
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "ERROR" and run.error_code == "tiktok_response_error"
        assert (
            run.work["stage"] == "ASSETS"
            and run.work["page"] == 2
            and run.work["bc_id"] == f"{bc}-1"
        )
        old = session.get(
            BCAccountAccess, (run.tenant_id, f"{bc}-1", "old", run.connection_id)
        )
        assert old.active
        assert (
            len(
                session.exec(
                    select(DiscoveryStagedPage).where(
                        DiscoveryStagedPage.run_id == run.id
                    )
                ).all()
            )
            == 5
        )
        run.status = "RUNNING"
        run.revision += 1
    fail["enabled"] = False
    for _ in range(18):
        step(contexts[0], runs[0], redis_client)
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "COMPLETE"
        assert (
            len(
                session.exec(
                    select(DiscoveryStagedPage).where(
                        DiscoveryStagedPage.run_id == run.id
                    )
                ).all()
            )
            == 15
        )
        assert not session.get(
            BCAccountAccess, (run.tenant_id, f"{bc}-1", "old", run.connection_id)
        ).active
    auth_calls = [c for c in calls if c[0].endswith("/oauth2/advertiser/get/")]
    assert len(auth_calls) == 1


def test_expired_claim_recovered_with_durable_watchdog(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        run.claim_id = uuid4()
        run.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
    step(contexts[0], runs[0], redis_client)
    assert len(calls) == 1
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.claim_id is None and run.work["stage"] == "BCS"
        dispatches = session.exec(
            select(PendingDispatch).where(PendingDispatch.tenant_id == run.tenant_id)
        ).all()
        assert any(
            ":recover:" in d.task_key and d.available_at > datetime.now(UTC)
            for d in dispatches
        )
        assert any(d.payload["revision"] == run.revision for d in dispatches)


def test_permission_revoked_before_next_call_stops_without_sending(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    step(contexts[0], runs[0], redis_client)
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (contexts[0].tenant_id, contexts[0].actor_id)
        ).active = False
    step(contexts[0], runs[0], redis_client)
    assert len(calls) == 1
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "ERROR" and run.error_code == "tenant_forbidden"


def test_cross_tenant_payload_does_not_query_sdk(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    with pytest.raises(DomainError) as error:
        step(contexts[1], runs[0], redis_client)
    assert error.value.code == "tenant_forbidden" and not calls


def test_stale_attempt_base_never_promotes(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        attempt = AuthorizationAttempt(
            tenant_id=run.tenant_id,
            actor_id=run.actor_id,
            connection_id=run.connection_id,
            state_hash=uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            status="CANDIDATE_READY",
            base_credential_revision=1,
            candidate_ciphertext=encrypt_credentials(
                tenant_id=run.tenant_id, value={"access_token": "stale-token"}
            ),
        )
        session.add(attempt)
        session.flush()
        run.candidate_attempt_id = attempt.id
    step(contexts[0], runs[0], redis_client)
    assert not calls
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.status == "ERROR" and run.error_code == "discovery_stale"
        assert session.get(TikTokConnection, run.connection_id).credential_revision == 0


def test_redis_unavailable_defers_without_sending(
    committed_runs, directory_transport, redis_client, monkeypatch
):
    from redis.exceptions import ConnectionError

    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("secret-redis-url")

    from redis import Redis

    monkeypatch.setattr(Redis, "execute_command", unavailable)
    step(contexts[0], runs[0], redis_client)
    assert not calls
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert (
            run.status == "ADMISSION_WAIT"
            and run.error_code == "tiktok_local_resources_unavailable"
            and run.sent_count == 0
        )


def test_too_short_lease_is_rejected_before_sdk(
    committed_runs, directory_transport, redis_client, monkeypatch, policy
):
    contexts, runs = committed_runs
    calls, _, _, _ = directory_transport
    policy.lease_ms = 50000
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", {"base": policy.model_dump()})
    step(contexts[0], runs[0], redis_client)
    assert not calls
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert run.error_code == "admission_policy_invalid" and run.sent_count == 0


def test_attempt_payload_creates_one_generation_and_resumes_failed_same_run(
    committed_runs, redis_client
):
    contexts, runs = committed_runs
    with Session(engine) as session, session.begin():
        existing = session.get(DiscoveryRun, runs[0])
        existing.status = "CANCELLED"
        attempt = AuthorizationAttempt(
            tenant_id=existing.tenant_id,
            actor_id=existing.actor_id,
            connection_id=existing.connection_id,
            state_hash=uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            status="CANDIDATE_READY",
            candidate_ciphertext=encrypt_credentials(
                tenant_id=existing.tenant_id, value={"access_token": "candidate"}
            ),
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
    payload = {"attempt_id": str(attempt_id)}
    for _ in range(2):
        tasks.process_discovery(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=contexts[0].tenant_id,
            actor_id=contexts[0].actor_id,
            payload=payload,
        )
    with Session(engine) as session, session.begin():
        found = session.exec(
            select(DiscoveryRun).where(DiscoveryRun.candidate_attempt_id == attempt_id)
        ).all()
        assert len(found) == 1
        new = found[0]
        runs.append(new.id)
        new.status = "ERROR"
        new.work = {**new.work, "stage": "BCS", "page": 2}
        identity = new.id
    tasks.process_discovery(
        database_engine=engine,
        redis_client=redis_client,
        tenant_id=contexts[0].tenant_id,
        actor_id=contexts[0].actor_id,
        payload=payload,
    )
    with Session(engine) as session:
        found = session.exec(
            select(DiscoveryRun).where(DiscoveryRun.candidate_attempt_id == attempt_id)
        ).all()
        assert (
            len(found) == 1 and found[0].id == identity and found[0].status == "RUNNING"
        )
        assert found[0].work["page"] == 2


def test_candidate_tenant_fk_prevents_cross_tenant_binding(committed_runs):
    contexts, runs = committed_runs
    with Session(engine) as session, session.begin():
        second = session.get(DiscoveryRun, runs[1])
        attempt = AuthorizationAttempt(
            tenant_id=second.tenant_id,
            actor_id=second.actor_id,
            connection_id=second.connection_id,
            state_hash=uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
    with Session(engine) as session:
        first = session.get(DiscoveryRun, runs[0])
        first.candidate_attempt_id = attempt_id
        with pytest.raises(IntegrityError):
            session.commit()


def test_handler_rejects_overridden_long_hard_deadline(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        tasks,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    tasks.discover.push_request(
        called_directly=False, is_eager=False, timelimit=(120, 40)
    )
    try:
        with pytest.raises(DomainError) as error:
            tasks.discover.run(
                tenant_id=str(uuid4()), actor_id=str(uuid4()), payload={}
            )
        assert error.value.code == "discovery_worker_unbounded"
    finally:
        tasks.discover.pop_request()


def test_historical_run_without_candidate_does_not_infer_current_route(
    committed_runs, directory_transport, redis_client
):
    contexts, runs = committed_runs
    with Session(engine) as session, session.begin():
        run = session.get(DiscoveryRun, runs[0])
        run.candidate_attempt_id = None
    step(contexts[0], runs[0], redis_client)
    assert not directory_transport[0]
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert (run.status, run.error_code) == ("ERROR", "discovery_stale")


def test_sdk_authorized_response_capacity_is_checked_before_parsing(
    committed_runs, directory_transport, redis_client, monkeypatch
):
    contexts, runs = committed_runs
    assert not directory_transport[0]
    body = json.dumps(
        {"code": 0, "data": {"list": [], "padding": "x" * (8 * 1024 * 1024)}}
    ).encode()
    sent = []

    def oversized(_pool, method, url, **_kwargs):
        sent.append((method, url))
        return HTTPResponse(
            body=body, status=200, headers={"Content-Type": "application/json"}
        )

    monkeypatch.setattr("urllib3.PoolManager.request", oversized)
    step(contexts[0], runs[0], redis_client)
    assert len(sent) == 1
    with Session(engine) as session:
        run = session.get(DiscoveryRun, runs[0])
        assert (run.status, run.error_code) == ("ERROR", "tiktok_response_error")
        assert not session.get(DiscoveryStagedPage, (run.id, "", "AUTHORIZED", 1))
