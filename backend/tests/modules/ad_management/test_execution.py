"""C5 execution tests use the repository PostgreSQL and Redis fixtures.

Only the provider transport is replaced: claims, attempts, receipts, route checks
and targeted refresh persistence stay on real database/Redis implementations.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError
from app.integrations.tiktok.contracts.management import ManagementReceipt
from app.jobs.models import PendingDispatch
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.management_capability_models import ManagementCapability
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.accounts.routing import freeze_route
from app.modules.ad_management.execution import execute_item
from app.modules.ad_management.models import (
    ManagementPreview,
    ManagementPreviewItem,
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.models import (
    ManagementReceipt as ReceiptRow,
)
from app.modules.ads.models import AdObject
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.conftest import create_context


@pytest.fixture
def c5_env():
    with Session(engine, expire_on_commit=False) as setup:
        context = create_context(setup)
        connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
        bc = TenantBC(tenant_id=context.tenant_id, bc_id="c5-bc")
        account = AdvertiserAccount(tenant_id=context.tenant_id, advertiser_id="c5-account", currency="USD", timezone="UTC", remote_status="STATUS_ENABLE")
        setup.add_all([connection, bc, account])
        setup.flush()
        now = datetime.now(UTC)
        setup.add_all([
            BCAccountAccess(tenant_id=context.tenant_id, bc_id=bc.bc_id, advertiser_id=account.advertiser_id, connection_id=connection.id, in_bc=True, authorized=True, active=True, can_build=True, can_upload=True, permission_state="VERIFIED", checked_at=now),
            BCConnectionBinding(tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=connection.id, kind=connection.kind),
            ConnectionAuthorization(tenant_id=context.tenant_id, connection_id=connection.id, authorization_revision=connection.authorization_revision, scopes=["read", "build"], permission_summary={"read_authorized": True, "build_authorized": True}, source="SYNTHETIC_COMPLETE_EVIDENCE", verified_at=now),
            BCDefaultRoute(tenant_id=context.tenant_id, bc_id=bc.bc_id, connection_id=connection.id),
        ])
        selection = FrozenSelectionRecord(tenant_id=context.tenant_id, bc_id=bc.bc_id, actor_id=context.actor_id, snapshot_id=uuid4(), advertiser_ids=[account.advertiser_id], filters={}, filter_digest="f" * 64, publication_versions={}, naming_versions={}, refs=[], material_uses=[], membership_digest="m" * 64)
        setup.add(selection)
        setup.flush()
        route = freeze_route(setup, context=context, bc_id=bc.bc_id)
        setup.commit()
        return context, bc, account, route, selection


class _Gateway:
    def __init__(self, apply):
        self.management = type("Management", (), {"apply": apply})()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _committed_item(management_env, *, original="20", final="25"):
    context, bc, account, route, selection = management_env
    with Session(engine, expire_on_commit=False) as setup:
        # ManagementCapability is the independent C2 permission evidence used by
        # the real route checker immediately before a physical request.
        setup.add(
            ManagementCapability(
                tenant_id=context.tenant_id,
                bc_id=bc.bc_id,
                advertiser_id=account.advertiser_id,
                connection_id=route.connection_id,
                authorization_revision=route.authorization_revision,
                binding_revision=route.binding_revision,
                adapter_contract_revision=route.adapter_contract_revision,
                operation="update_budget",
                entity_kind="adgroup",
                state="VERIFIED",
                verified_at=datetime.now(UTC),
                evidence={"source": "local-test"},
            )
        )
        preview = ManagementPreview(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            selection_id=selection.id,
            digest="e" * 64,
            mutation={"field": "budget"},
            route=route.model_dump(mode="json"),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            status="READY",
            counts={"selected": 1, "targets": 1, "linked": 0, "unsupported": 0},
        )
        setup.add(preview)
        setup.add(
            AdObject(
                tenant_id=context.tenant_id,
                advertiser_id=account.advertiser_id,
                kind="adgroup",
                remote_id="group-c5",
                ad_type="REGULAR",
                configuration={"budget": original},
                operation_status="ENABLE",
                delivery_status="DELIVERING",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=route.connection_id,
                source_channel=route.channel,
            )
        )
        setup.flush()
        preview_item = ManagementPreviewItem(
            tenant_id=context.tenant_id,
            preview_id=preview.id,
            position=0,
            ref={
                "tenant_id": str(context.tenant_id),
                "advertiser_id": account.advertiser_id,
                "kind": "adgroup",
                "remote_id": "group-c5",
            },
            original_value=original,
            final_value=final,
            execution_result="PENDING",
            capability={
                "operation": "update_budget",
                "entity_kind": "adgroup",
                "ad_type": "REGULAR",
                "configuration": {"budget": original},
                "source_connection_id": str(route.connection_id),
                "source_channel": route.channel,
                "route": route.model_dump(mode="json"),
            },
        )
        setup.add(preview_item)
        setup.flush()
        task = ManagementTask(
            tenant_id=context.tenant_id,
            bc_id=bc.bc_id,
            actor_id=context.actor_id,
            preview_id=preview.id,
            idempotency_key=uuid4(),
            digest=preview.digest,
            route=route.model_dump(mode="json"),
            status="QUEUED",
            counts=preview.counts,
        )
        setup.add(task)
        setup.flush()
        item = ManagementTaskItem(
            tenant_id=context.tenant_id,
            task_id=task.id,
            preview_item_id=preview_item.id,
            preview_id=preview.id,
            position=0,
            ref=dict(preview_item.ref),
            original_value=original,
            final_value=final,
            execution_result="PENDING",
            capability=dict(preview_item.capability),
        )
        setup.add(item)
        setup.commit()
        return context, task.id, item.id


def _cleanup(task_id):
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        if task is not None:
            session.delete(task)
        for dispatch in session.exec(
            select(PendingDispatch).where(PendingDispatch.task_name == "ad_management.execute")
        ).all():
            if dispatch.payload.get("task_id") == str(task_id):
                session.delete(dispatch)
        session.commit()


def test_accepted_write_commits_receipt_without_synchronous_readback(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []
    refreshes = []

    def apply(_self, command):
        calls.append(command)
        return ManagementReceipt("ACCEPTED", "request-c5-accepted", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    monkeypatch.setattr(
        "app.modules.ad_management.execution.request_sync",
        lambda _session, *, context, request: refreshes.append((context, request)) or uuid4(),
    )
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "ACCEPTED"
            assert len(calls) == 1
            assert len(session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).all()) == 1
            receipt = session.exec(select(ReceiptRow).where(ReceiptRow.tenant_id == context.tenant_id)).one()
            assert receipt.outcome == "ACCEPTED"
            assert len(refreshes) == 1
            assert refreshes[0][1].scope == "targeted"
            assert refreshes[0][1].start_date is None and refreshes[0][1].end_date is None
    finally:
        _cleanup(task_id)


def test_unknown_result_is_terminal_and_is_never_resent(
    c5_env, redis_client, monkeypatch
):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    def apply(_self, _command):
        calls.append(True)
        raise RemoteCallError(
            "mcp_call_failed", effect="UNKNOWN", evidence=CallEvidence(request_id="unknown-c5")
        )

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr(
        "app.modules.ad_management.execution.open_tiktok_gateway",
        lambda **_kwargs: _Gateway(apply),
    )
    try:
        execute_item(engine, item_id)
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempts = session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).all()
            assert len(attempts) == 1
            assert attempts[0].outcome == "UNKNOWN"
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_targeted_refresh_failure_is_durable_and_retry_does_not_resend(c5_env, redis_client, monkeypatch):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    refreshes = []
    failed = {"once": True}

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "refresh-retry", False)

    def request_refresh(_session, *, context, request):
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("synthetic refresh outage")
        refreshes.append((context, request))
        return uuid4()

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    monkeypatch.setattr("app.modules.ad_management.execution.request_sync", request_refresh)
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None
            assert item.execution_result == "ACCEPTED"
            assert item.observation_state == "REFRESH_PENDING"
            assert session.exec(
                select(PendingDispatch).where(
                    PendingDispatch.tenant_id == item.tenant_id,
                    PendingDispatch.task_name == "ad_management.execute",
                    PendingDispatch.payload["task_id"].as_string() == str(item.task_id),
                )
            ).first() is not None
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None
            assert item.execution_result == "ACCEPTED"
            assert item.observation_state == "ACCEPTED"
        assert calls == [True]
        assert len(refreshes) == 1
    finally:
        _cleanup(task_id)


def test_revoked_management_capability_blocks_request_after_claim(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []
    with Session(engine) as session:
        capability = session.exec(
            select(ManagementCapability).where(
                ManagementCapability.tenant_id == context.tenant_id,
                ManagementCapability.operation == "update_budget",
            )
        ).one()
        capability.state = "REVOKED"
        session.add(capability)
        session.commit()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "should-not-send", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.claim_token is None
        assert calls == []
    finally:
        _cleanup(task_id)


def test_missing_current_object_fails_closed_before_provider(c5_env, redis_client, monkeypatch):
    _context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        session.delete(session.get(AdObject, (_context.tenant_id, "c5-account", "adgroup", "group-c5")))
        session.commit()
    calls = []
    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)))
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.reason == "management_observation_conflict"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_frozen_route_revision_change_blocks_without_channel_fallback(c5_env, redis_client, monkeypatch):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        assert task is not None
        task.route = {**task.route, "authorization_revision": task.route["authorization_revision"] + 1}
        session.add(task)
        session.commit()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "should-not-send", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "NOT_SENT"
            assert item.reason == "frozen_route_changed"
            assert item.claim_token is None
        assert calls == []
    finally:
        _cleanup(task_id)


def test_one_unknown_item_does_not_block_sibling(c5_env, redis_client, monkeypatch):
    context, task_id, first_id = _committed_item(c5_env)
    with Session(engine) as session:
        first = session.get(ManagementTaskItem, first_id)
        assert first is not None
        second = ManagementTaskItem(
            tenant_id=first.tenant_id,
            task_id=first.task_id,
            preview_item_id=first.preview_item_id,
            preview_id=first.preview_id,
            position=1,
            ref={**first.ref, "remote_id": "group-c5-sibling"},
            original_value=first.original_value,
            final_value=first.final_value,
            execution_result="PENDING",
            capability=dict(first.capability),
        )
        session.add(second)
        session.add(
            AdObject(
                tenant_id=first.tenant_id,
                advertiser_id=first.ref["advertiser_id"],
                kind="adgroup",
                remote_id="group-c5-sibling",
                ad_type="REGULAR",
                configuration={"budget": first.original_value},
                operation_status="ENABLE",
                delivery_status="DELIVERING",
                observed_at=datetime.now(UTC),
                published_version=1,
                source_connection_id=c5_env[3].connection_id,
                source_channel=c5_env[3].channel,
            )
        )
        session.commit()
        second_id = second.id
    calls = []

    def apply(_self, command):
        calls.append(command.ref.remote_id)
        if command.ref.remote_id == "group-c5":
            raise RemoteCallError("mcp_call_failed", effect="UNKNOWN", evidence=CallEvidence(request_id="unknown-sibling"))
        return ManagementReceipt("ACCEPTED", "accepted-sibling", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    try:
        execute_item(engine, first_id)
        execute_item(engine, second_id)
        with Session(engine) as session:
            assert session.get(ManagementTaskItem, first_id).execution_result == "UNKNOWN"
            assert session.get(ManagementTaskItem, second_id).execution_result == "ACCEPTED"
        assert calls == ["group-c5", "group-c5-sibling"]
    finally:
        _cleanup(task_id)


def test_receipt_commit_failure_is_fenced_unknown_and_not_replayed(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "accepted-before-db-failure", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    original_persist = __import__("app.modules.ad_management.execution", fromlist=["_persist_outcome"])._persist_outcome
    failed = {"once": True}

    def fail_once(*args, **kwargs):
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("synthetic receipt commit failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr("app.modules.ad_management.execution._persist_outcome", fail_once)
    try:
        execute_item(engine, item_id)
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempt = session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).one()
            assert attempt.outcome == "UNKNOWN"
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_management_queue_is_registered_with_one_slot():
    from app.jobs.celery_app import celery_app
    from app.jobs.tasks import dispatch_queue

    assert dispatch_queue("ad_management.execute") == "ad-management"
    assert celery_app.conf.task_routes["ad_management.execute"]["queue"] == "ad-management"
    assert "--concurrency 1" in open("deploy/staging-ad-management.service").read()


def test_terminal_task_dispatches_durable_refresh_pending_item(c5_env, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        task = session.get(ManagementTask, task_id)
        assert item is not None and task is not None
        item.execution_result = "ACCEPTED"
        item.observation_state = "REFRESH_PENDING"
        task.status = "SUCCEEDED"
        session.add_all([item, task])
        session.commit()
    calls = []
    monkeypatch.setattr("app.modules.ad_management.tasks.execute_item", lambda _engine, current_id: calls.append(current_id))
    from app.modules.ad_management.tasks import execute_management_task

    try:
        execute_management_task(
            tenant_id=str(context.tenant_id),
            actor_id=str(context.actor_id),
            payload={"task_id": str(task_id), "generation": 1},
        )
        assert calls == [item_id]
    finally:
        _cleanup(task_id)


def test_stale_lease_cannot_write_provider_receipt(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    calls = []

    class StaleAfterSendLease:
        def __init__(self):
            self.checks = 0

        def fence(self, *_args):
            self.checks += 1
            if self.checks >= 3:
                from app.core.errors import DomainError
                raise DomainError("mutation_lease_stale", "stale")

        def release(self, *_args):
            return None

    lease = StaleAfterSendLease()

    def apply(_self, _command):
        calls.append(True)
        return ManagementReceipt("ACCEPTED", "accepted-stale", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.claim_mutation", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "UNKNOWN"
            attempt = session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).one()
            assert attempt.outcome == "UNKNOWN"
        assert calls == [True]
        execute_item(engine, item_id)
        assert calls == [True]
    finally:
        _cleanup(task_id)


def test_expired_pre_attempt_recovers_unknown_receipt_without_resend(c5_env, redis_client, monkeypatch):
    context, task_id, item_id = _committed_item(c5_env)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item is not None
        item.claim_generation = 4
        item.claim_token = uuid4()
        item.claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        item.delivery_status = "ATTEMPT_RECORDED"
        session.add(
            ManagementRequestAttempt(
                tenant_id=context.tenant_id,
                task_item_id=item_id,
                attempt=1,
                outcome="NOT_SENT",
                payload={},
                response={},
                retryable=False,
            )
        )
        session.add(item)
        session.commit()
    calls = []
    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(lambda *_args: calls.append(True)))
    try:
        execute_item(engine, item_id)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            attempt = session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).one()
            receipt = session.exec(select(ReceiptRow).where(ReceiptRow.attempt_id == attempt.id)).one()
            assert item is not None and item.execution_result == "UNKNOWN"
            assert attempt.outcome == "UNKNOWN"
            assert receipt.outcome == "UNKNOWN"
        assert calls == []
    finally:
        _cleanup(task_id)


def test_duplicate_delivery_sends_once_while_first_provider_call_is_in_flight(c5_env, redis_client, monkeypatch):
    _context, task_id, item_id = _committed_item(c5_env)
    calls = []
    provider_started = Event()
    release_provider = Event()

    def apply(_self, _command):
        calls.append(True)
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ManagementReceipt("ACCEPTED", "duplicate-once", False)

    monkeypatch.setattr("app.modules.ad_management.execution._redis", lambda: redis_client)
    monkeypatch.setattr("app.modules.ad_management.execution.open_tiktok_gateway", lambda **_kwargs: _Gateway(apply))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(execute_item, engine, item_id)
            assert provider_started.wait(timeout=5)
            second = pool.submit(execute_item, engine, item_id)
            second.result(timeout=5)
            release_provider.set()
            first.result(timeout=5)
        with Session(engine) as session:
            item = session.get(ManagementTaskItem, item_id)
            assert item is not None and item.execution_result == "ACCEPTED"
            assert len(session.exec(select(ManagementRequestAttempt).where(ManagementRequestAttempt.task_item_id == item_id)).all()) == 1
        assert calls == [True]
    finally:
        _cleanup(task_id)
