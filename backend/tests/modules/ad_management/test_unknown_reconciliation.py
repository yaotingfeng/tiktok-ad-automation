from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok.contracts.ads import AdEntity, DirectoryPage, EntityRef
from app.integrations.tiktok.contracts.common import CallEvidence
from app.modules.ad_management.direct_submission import submit_direct_task
from app.modules.ad_management.models import (
    ManagementRequestAttempt,
    ManagementTask,
    ManagementTaskItem,
)
from app.modules.ad_management.preparation import prepare_direct_task
from app.modules.ad_management.reconciliation import (
    queue_reconciliation,
    reconcile_unknown_items,
)
from app.modules.ad_management.schemas import MutationSpec
from app.modules.reporting.query_models import FrozenSelectionRecord
from tests.modules.ad_management.conftest import _row, _set_selection
from tests.modules.ad_management.test_submission_concurrency import (
    committed_management_env as committed_management_env,
)


@pytest.fixture
def unknown_task(committed_management_env):
    context, bc, account, route, selection = committed_management_env
    ref = EntityRef(
        context.tenant_id, account.advertiser_id, "campaign", "unknown-read"
    )
    with Session(engine) as session:
        row = session.get(FrozenSelectionRecord, selection.id)
        _set_selection(row, [ref])
        session.add(row)
        session.add(_row(ref, connection_id=route.connection_id))
        session.flush()
        task_id = submit_direct_task(
            session,
            context,
            bc.bc_id,
            row.id,
            MutationSpec(field="status", mode="set", value="DISABLE"),
            uuid4(),
        ).task_id
        session.commit()
    prepare_direct_task(engine, task_id)
    with Session(engine) as session:
        item = session.exec(
            select(ManagementTaskItem).where(ManagementTaskItem.task_id == task_id)
        ).one()
        item.execution_result = "UNKNOWN"
        item.request_attribution = "original-request"
        task = session.get(ManagementTask, task_id)
        task.status = "NEEDS_REVIEW"
        session.add_all(
            [
                item,
                task,
                ManagementRequestAttempt(
                    tenant_id=context.tenant_id,
                    task_item_id=item.id,
                    outcome="UNKNOWN",
                    request_id="original-request",
                ),
            ]
        )
        session.flush()
        queue_reconciliation(session, task, (item.id,))
        item_id = item.id
        session.commit()
    return context, route, task_id, item_id, ref


@pytest.mark.parametrize(
    "status, expected",
    [("DISABLE", "OBSERVED_MATCH"), ("ENABLE", "OBSERVED_DIFFERENT")],
)
def test_unknown_reads_platform_without_claiming_acceptance(
    unknown_task, monkeypatch, status, expected
):
    context, route, task_id, item_id, ref = unknown_task
    calls = []

    def read(query):
        calls.append(query)
        return DirectoryPage(
            items=(
                AdEntity(
                    ref,
                    None,
                    "REGULAR",
                    "name",
                    {},
                    status,
                    None,
                    None,
                    datetime.now(UTC),
                ),
            ),
            materials=(),
            complete=True,
            next_page=None,
            evidence=CallEvidence(request_id="read-request"),
        )

    @contextmanager
    def gateway(**kwargs):
        assert kwargs["route"] == route
        kwargs["before_request"]()
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", gateway
    )
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
    )
    assert len(calls) == 1 and calls[0].ids == (ref.remote_id,)
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert item.execution_result == "UNKNOWN" and item.observation_state == expected
        assert item.request_attribution == "original-request"
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
    )
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["stale", "missing", "incomplete"])
def test_missing_or_stale_evidence_retries_three_times_then_stops(
    unknown_task, monkeypatch, failure
):
    from app.jobs.models import PendingDispatch

    context, _, task_id, item_id, ref = unknown_task
    calls = []

    def read(query):
        calls.append(query)
        entity = AdEntity(
            ref,
            None,
            "REGULAR",
            "name",
            {},
            "DISABLE",
            None,
            None,
            datetime.now(UTC) - timedelta(hours=1)
            if failure == "stale"
            else datetime.now(UTC),
        )
        return DirectoryPage(
            items=() if failure == "missing" else (entity,),
            materials=(),
            complete=failure != "incomplete",
            next_page=None,
            evidence=CallEvidence(request_id="read-failure"),
        )

    @contextmanager
    def gateway(**kwargs):
        kwargs["before_request"]()
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", gateway
    )
    for attempt in range(1, 4):
        reconcile_unknown_items(
            engine, context.tenant_id, context.actor_id, task_id, (item_id,), attempt
        )
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert (
            item.execution_result == "UNKNOWN"
            and item.observation_state == "RECONCILE_FAILED"
        )
        messages = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "ad_management.reconcile",
            )
        ).all()
        assert sorted(message.payload["attempt"] for message in messages) == [1, 2, 3]
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 3
    )
    assert len(calls) == 3


def test_remote_retry_after_is_preserved(unknown_task, monkeypatch):
    from app.integrations.tiktok.contracts.common import RemoteCallError
    from app.jobs.models import PendingDispatch

    context, _, task_id, item_id, _ = unknown_task

    @contextmanager
    def gateway(**_kwargs):
        raise RemoteCallError(
            "admission_deferred",
            effect="NOT_SENT",
            evidence=CallEvidence(),
            retry_after_ms=300_000,
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", gateway
    )
    started = datetime.now(UTC)
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
    )
    with Session(engine) as session:
        message = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "ad_management.reconcile",
                PendingDispatch.payload["attempt"].as_integer() == 2,
            )
        ).one()
        assert message.available_at >= started + timedelta(seconds=300)


@pytest.mark.parametrize("change", ["route", "permission"])
def test_revoked_frozen_route_or_permission_never_calls_provider(
    unknown_task, monkeypatch, change
):
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    context, route, task_id, item_id, _ = unknown_task
    with Session(engine) as session:
        if change == "route":
            row = session.get(TikTokConnection, route.connection_id)
            row.authorization_revision += 1
        else:
            row = session.get(
                BCAccountAccess,
                (
                    context.tenant_id,
                    route.bc_id,
                    "management-race-account",
                    route.connection_id,
                ),
            )
            row.authorized = False
        session.add(row)
        session.commit()

    def forbidden(**_kwargs):
        pytest.fail("revoked scope must not read provider")

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", forbidden
    )
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
    )
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert (
            item.execution_result == "UNKNOWN"
            and item.observation_state == "RECONCILE_FAILED"
        )


def test_queue_rejects_cross_task_and_manual_round_can_recheck(unknown_task):
    from app.core.errors import DomainError
    from app.jobs.models import PendingDispatch
    from app.modules.ad_management.reconciliation import reconcile_item

    context, _, task_id, item_id, _ = unknown_task
    with Session(engine) as session:
        task = session.get(ManagementTask, task_id)
        with pytest.raises(DomainError):
            queue_reconciliation(session, task, (uuid4(),))
        item = session.get(ManagementTaskItem, item_id)
        item.observation_state = "OBSERVED_MATCH"
        session.add(item)
        session.flush()
        queue_reconciliation(session, task, (item_id,))
        assert item.observation_state == "OBSERVED_MATCH"
        reconcile_item(session, context, item_id)
        session.commit()
        assert item.observation_state == "RECONCILE_REQUESTED"
        messages = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == context.tenant_id,
                PendingDispatch.task_name == "ad_management.reconcile",
            )
        ).all()
        assert (
            len(messages) == 2
            and len({message.payload["round"] for message in messages}) == 2
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": str(uuid4()), "item_ids": [], "attempt": 1, "round": "auto"},
        {
            "task_id": str(uuid4()),
            "item_ids": [str(uuid4())],
            "attempt": 4,
            "round": "auto",
        },
        {
            "task_id": str(uuid4()),
            "item_ids": [str(uuid4())],
            "attempt": True,
            "round": "auto",
        },
        {
            "task_id": str(uuid4()),
            "item_ids": [str(uuid4())],
            "attempt": 1,
            "round": "bad",
        },
    ],
)
def test_reconciliation_task_rejects_invalid_payload(payload):
    from app.core.errors import DomainError
    from app.modules.ad_management.reconciliation_tasks import reconcile_management_task

    with pytest.raises(DomainError):
        reconcile_management_task(
            tenant_id=str(uuid4()), actor_id=str(uuid4()), payload=payload
        )


def test_material_reconciliation_uses_exact_material_identity(
    unknown_task, monkeypatch
):
    from app.integrations.tiktok.contracts.ads import AdMaterialUsage, MaterialUseRef

    context, _, task_id, item_id, old_ref = unknown_task
    ref = EntityRef(context.tenant_id, old_ref.advertiser_id, "ad", "material-ad")
    use = MaterialUseRef(ref, "video-1", "material-1", "VIDEO")
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        item.ref = {
            "tenant_id": str(ref.tenant_id),
            "advertiser_id": ref.advertiser_id,
            "kind": ref.kind,
            "remote_id": ref.remote_id,
        }
        item.material_use = {
            "ad_ref": item.ref,
            "platform_material_id": use.platform_material_id,
            "ad_material_id": use.ad_material_id,
            "material_type": use.material_type,
        }
        item.capability = {
            **item.capability,
            "operation": "set_material_status",
            "ad_type": "SMART_PLUS",
        }
        session.add(item)
        session.commit()

    def read(query):
        assert query.ids == (ref.remote_id,) and query.ad_type == "SMART_PLUS"
        return DirectoryPage(
            items=(
                AdEntity(
                    ref,
                    None,
                    "SMART_PLUS",
                    "ad",
                    {},
                    "DISABLE",
                    None,
                    None,
                    datetime.now(UTC),
                ),
            ),
            materials=(
                AdMaterialUsage(
                    MaterialUseRef(ref, "video-1", "other-id", "VIDEO"),
                    None,
                    "DISABLE",
                    True,
                ),
                AdMaterialUsage(use, None, "ENABLE", True),
            ),
            complete=True,
            next_page=None,
            evidence=CallEvidence(request_id="material-read"),
        )

    @contextmanager
    def gateway(**_kwargs):
        yield SimpleNamespace(ads=SimpleNamespace(read_page=read))

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", gateway
    )
    reconcile_unknown_items(
        engine, context.tenant_id, context.actor_id, task_id, (item_id,), 1
    )
    with Session(engine) as session:
        item = session.get(ManagementTaskItem, item_id)
        assert (
            item.observation_state == "OBSERVED_DIFFERENT"
            and item.execution_result == "UNKNOWN"
        )


def test_wrong_actor_cannot_read_unknown_task(unknown_task, monkeypatch):
    from app.core.errors import DomainError

    context, _, task_id, item_id, _ = unknown_task

    def forbidden(**_kwargs):
        pytest.fail("wrong actor must not reach provider")

    monkeypatch.setattr(
        "app.modules.ad_management.reconciliation.open_tiktok_gateway", forbidden
    )
    with pytest.raises(DomainError):
        reconcile_unknown_items(
            engine, context.tenant_id, uuid4(), task_id, (item_id,), 1
        )
