"""复制聚合合同：数值精确、范围不扩张、重试不会重算覆盖。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.campaign_copies.schemas import CopyConfiguration, CopyPatchRequest


def configuration(**changes):
    return CopyConfiguration.model_validate(
        {
            "target_advertiser_ids": ["a"],
            "copies": 1,
            "material_ids": [str(uuid4())],
        }
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"budget": 1.1},
        {"target_roas": True},
        {"budget": "NaN"},
        {"budget": "0"},
        {"copies": True},
        {"copies": 21},
        {"target_advertiser_ids": []},
        {"material_ids": []},
    ],
)
def test_copy_configuration_rejects_invalid_overrides(changes):
    with pytest.raises(ValidationError):
        configuration(**changes)


def test_copy_configuration_preserves_inherit_and_deduplicates():
    value = configuration(target_advertiser_ids=["a", "a", "b"])
    assert value.target_advertiser_ids == ["a", "b"]
    assert value.budget is None and value.target_roas is None
    assert value.delivery_config.schedule_mode == "IMMEDIATE"


def test_copy_patch_cannot_repeat_a_source_row():
    row = {
        "row_id": str(uuid4()),
        "configuration": configuration().model_dump(mode="json"),
    }
    with pytest.raises(ValidationError):
        CopyPatchRequest(request_id=uuid4(), expected_revision=1, rows=[row, row])


@pytest.fixture
def queue_fixture(session, context, intent, monkeypatch):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.campaign_copies import service
    from tests.modules.builds.test_drafts import account

    account(session, context)
    account(session, context, "account-B")
    material = str(uuid4())
    source = {
        "advertiser_id": "account-A",
        "campaign_id": "campaign-source",
        "name": "Source",
        "currency": "USD",
        "budget": "100",
        "target_roas": "1.1",
        "budget_strategy": "SERIES",
        "bid_strategy": "TARGET_ROAS",
        "materials": [
            {"material_id": material, "file_name": "source.mp4", "media_type": "VIDEO"}
        ],
        "targeting": None,
        "blockers": [],
        "snapshot": {"private_provenance": "not-public"},
    }
    monkeypatch.setattr(
        service,
        "get_frozen_selection",
        lambda *args, **kw: SimpleNamespace(
            refs=(
                EntityRef(
                    context.tenant_id, "account-A", "campaign", "campaign-source"
                ),
            ),
            material_uses=(),
        ),
    )
    monkeypatch.setattr(
        "app.modules.builds.copy_source.load_copy_source",
        lambda *args, **kw: dict(source),
    )
    request_id = uuid4()
    row = service.create(
        session,
        context=context,
        bc_id=intent["bc_id"],
        selection_id=uuid4(),
        request_id=request_id,
    )
    return service, row, source, material


def test_create_freezes_selection_and_hides_internal_snapshot(queue_fixture):
    service, row, source, material = queue_fixture
    assert row.rows[0]["source"]["snapshot"] == source["snapshot"]
    result = service.public(row)
    assert "snapshot" not in result.rows[0].source
    assert result.rows[0].configuration["material_ids"] == [material]
    assert result.rows[0].configuration["budget"] is None
    assert result.status == "DRAFT" and result.children == []


def test_patch_revision_idempotency_and_material_scope(session, context, queue_fixture):
    from app.core.errors import DomainError

    service, row, _, material = queue_fixture
    request = CopyPatchRequest(
        request_id=uuid4(),
        expected_revision=1,
        rows=[
            {
                "row_id": row.rows[0]["row_id"],
                "configuration": configuration(
                    material_ids=[material],
                    target_advertiser_ids=["account-A", "account-B"],
                    copies=2,
                ).model_dump(mode="json"),
            }
        ],
    )
    saved = service.patch(session, context=context, draft_id=row.id, body=request)
    assert saved.revision == 2
    assert (
        service.patch(session, context=context, draft_id=row.id, body=request).revision
        == 2
    )
    assert saved.rows[0]["configuration"]["target_advertiser_ids"] == [
        "account-A",
        "account-B",
    ]
    stale = request.model_copy(update={"request_id": uuid4()})
    with pytest.raises(DomainError, match="草稿已更新"):
        service.patch(session, context=context, draft_id=row.id, body=stale)
    invalid = CopyPatchRequest(
        request_id=uuid4(),
        expected_revision=2,
        rows=[
            {
                "row_id": row.rows[0]["row_id"],
                "configuration": configuration(material_ids=[uuid4()]).model_dump(
                    mode="json"
                ),
            }
        ],
    )
    with pytest.raises(DomainError, match="只能选择"):
        service.patch(session, context=context, draft_id=row.id, body=invalid)


def test_preview_expands_source_account_ordinal_once(session, context, queue_fixture):
    from sqlmodel import select

    from app.jobs.models import PendingDispatch
    from app.modules.campaign_copies.schemas import CopyRevisionRequest

    service, row, _, material = queue_fixture
    request = CopyPatchRequest(
        request_id=uuid4(),
        expected_revision=1,
        rows=[
            {
                "row_id": row.rows[0]["row_id"],
                "configuration": configuration(
                    material_ids=[material],
                    target_advertiser_ids=["account-A", "account-B"],
                    copies=2,
                ).model_dump(mode="json"),
            }
        ],
    )
    service.patch(session, context=context, draft_id=row.id, body=request)
    preview = CopyRevisionRequest(request_id=uuid4(), expected_revision=2)
    prepared = service.preview(session, context=context, draft_id=row.id, body=preview)
    assert prepared.status == "PREPARING"
    assert [(c["advertiser_id"], c["ordinal"]) for c in prepared.children] == [
        ("account-A", 1),
        ("account-A", 2),
        ("account-B", 1),
        ("account-B", 2),
    ]
    dispatch = prepared.dispatch_id
    assert (
        service.preview(
            session, context=context, draft_id=row.id, body=preview
        ).dispatch_id
        == dispatch
    )
    assert (
        len(
            session.exec(
                select(PendingDispatch).where(PendingDispatch.id == dispatch)
            ).all()
        )
        == 1
    )


def test_copy_permissions_rechecked_on_retry_and_other_tenant_hidden(
    session, context, other_context, queue_fixture
):
    from app.core.errors import DomainError
    from app.modules.tenants.models import TenantMembership

    service, row, _, _ = queue_fixture
    with pytest.raises(DomainError, match="不存在"):
        service.get_draft(session, context=other_context, draft_id=row.id)
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert service.get_draft(session, context=context, draft_id=row.id).id == row.id
    with pytest.raises(DomainError, match="当前角色"):
        service.get_draft(session, context=context, draft_id=row.id, write=True)


def test_preview_source_blockers_do_not_get_silently_skipped(
    session, context, queue_fixture
):
    from app.core.errors import DomainError
    from app.modules.campaign_copies.schemas import CopyRevisionRequest

    service, row, source, _ = queue_fixture
    row.rows = [
        dict(row.rows[0], source=dict(source, blockers=["当前不支持复制此来源"]))
    ]
    session.add(row)
    session.flush()
    with pytest.raises(DomainError, match="来源配置不完整"):
        service.preview(
            session,
            context=context,
            draft_id=row.id,
            body=CopyRevisionRequest(request_id=uuid4(), expected_revision=1),
        )


def test_copy_api_current_bc_and_viewer_write_gate(
    client, session, context, queue_fixture
):
    from app.modules.tenants.models import TenantMembership
    from tests.modules.strategies.test_api import headers

    _, row, _, _ = queue_fixture
    path = f"/api/tenants/{context.tenant_id}/campaign-copies/{row.id}"
    assert (
        client.get(
            path, params={"bc_id": "other"}, headers=headers(context)
        ).status_code
        == 404
    )
    assert (
        client.get(
            path, params={"bc_id": row.bc_id}, headers=headers(context)
        ).status_code
        == 200
    )
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    assert (
        client.post(
            path + "/preview",
            json={"request_id": str(uuid4()), "expected_revision": 1},
            headers=headers(context),
        ).status_code
        == 403
    )


def test_copy_expansion_limit_and_currency_are_enforced(
    session, context, queue_fixture
):
    from copy import deepcopy

    from app.core.errors import DomainError
    from app.modules.accounts.models import AdvertiserAccount

    service, row, _, _ = queue_fixture
    members = []
    for _ in range(51):
        item = deepcopy(row.rows[0])
        item["row_id"] = str(uuid4())
        item["configuration"]["copies"] = 20
        members.append(item)
    with pytest.raises(DomainError, match="1000"):
        service._validate_rows(session, context, row, members, strict=True)
    account = session.get(AdvertiserAccount, (context.tenant_id, "account-B"))
    account.currency = "EUR"
    session.add(account)
    session.flush()
    members = deepcopy(row.rows)
    members[0]["configuration"]["target_advertiser_ids"] = ["account-B"]
    with pytest.raises(DomainError, match="币种"):
        service._validate_rows(session, context, row, members, strict=True)


def test_request_identity_cannot_be_rebound(session, context, queue_fixture):
    from app.core.errors import DomainError

    service, row, _, material = queue_fixture
    request = CopyPatchRequest(
        request_id=uuid4(),
        expected_revision=1,
        rows=[
            {
                "row_id": row.rows[0]["row_id"],
                "configuration": configuration(
                    material_ids=[material], target_advertiser_ids=["account-A"]
                ).model_dump(mode="json"),
            }
        ],
    )
    service.patch(session, context=context, draft_id=row.id, body=request)
    changed = request.model_copy(deep=True)
    changed.rows[0].configuration.copies = 2
    with pytest.raises(DomainError, match="请求标识"):
        service.patch(session, context=context, draft_id=row.id, body=changed)


def _committed_queue(engine, context, monkeypatch):
    from sqlmodel import Session

    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.campaign_copies import service
    from tests.modules.builds.test_drafts import account, create_intent

    material = str(uuid4())
    source = {
        "advertiser_id": "account-A",
        "campaign_id": "campaign-source",
        "name": "Source",
        "currency": "USD",
        "budget": "100",
        "target_roas": "1.1",
        "budget_strategy": "SERIES",
        "bid_strategy": "TARGET_ROAS",
        "materials": [
            {"material_id": material, "file_name": "source.mp4", "media_type": "VIDEO"}
        ],
        "targeting": None,
        "blockers": [],
        "snapshot": {},
    }
    monkeypatch.setattr(
        service,
        "get_frozen_selection",
        lambda *args, **kw: SimpleNamespace(
            refs=(
                EntityRef(
                    context.tenant_id, "account-A", "campaign", "campaign-source"
                ),
            ),
            material_uses=(),
        ),
    )
    monkeypatch.setattr(
        "app.modules.builds.copy_source.load_copy_source",
        lambda *args, **kw: dict(source),
    )
    with Session(engine) as session, session.begin():
        intent = create_intent(session, context)
        account(session, context)
        row = service.create(
            session,
            context=context,
            bc_id=intent["bc_id"],
            selection_id=uuid4(),
            request_id=uuid4(),
        )
        return row.id, row.rows[0]["row_id"], material


def test_concurrent_copy_patch_applies_once(isolated_strategy_database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.campaign_copies import service

    engine, context, _ = isolated_strategy_database
    identity, row_id, material = _committed_queue(engine, context, monkeypatch)
    request = CopyPatchRequest(
        request_id=uuid4(),
        expected_revision=1,
        rows=[
            {
                "row_id": row_id,
                "configuration": configuration(
                    material_ids=[material],
                    target_advertiser_ids=["account-A"],
                    copies=2,
                ).model_dump(mode="json"),
            }
        ],
    )
    barrier = Barrier(2)

    def work():
        barrier.wait()
        with Session(engine) as session, session.begin():
            return service.patch(
                session, context=context, draft_id=identity, body=request
            ).revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: work(), range(2))) == [2, 2]
    with Session(engine) as session:
        assert (
            service.get_draft(session, context=context, draft_id=identity).rows[0][
                "configuration"
            ]["copies"]
            == 2
        )


def test_pending_copy_reentry_and_control_repair_are_durable(
    isolated_strategy_database, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session

    from app.core.errors import DomainError
    from app.jobs.models import PendingDispatch
    from app.modules.campaign_copies import service, tasks
    from app.modules.campaign_copies.models import CampaignCopyDraft
    from app.modules.campaign_copies.schemas import CopyRevisionRequest

    engine, context, _ = isolated_strategy_database
    identity, _, _ = _committed_queue(engine, context, monkeypatch)
    attempts = []

    def pending(*_args, **kw):
        attempts.append(kw["request_id"])
        raise DomainError("copy_preparing", "waiting for scene")

    monkeypatch.setattr("app.modules.builds.copying.create_copy_preview", pending)
    with Session(engine) as session, session.begin():
        row = service.preview(
            session,
            context=context,
            draft_id=identity,
            body=CopyRevisionRequest(request_id=uuid4(), expected_revision=1),
        )
        payload = {"draft_id": str(identity), "generation": row.generation}
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    assert len(attempts) == 1
    with Session(engine) as session, session.begin():
        row = session.get(CampaignCopyDraft, identity)
        assert row.status == "PREPARING" and row.generation == payload["generation"] + 1
        next_payload = {"draft_id": str(identity), "generation": row.generation}
        dispatch_id = row.dispatch_id
        row.repair_after = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        dispatch = session.get(PendingDispatch, dispatch_id)
        dispatch.published_at = datetime.now(UTC)
        session.add(dispatch)
    assert tasks.repair(database_engine=engine) == 1
    with Session(engine) as session:
        assert session.get(CampaignCopyDraft, identity).dispatch_id == dispatch_id
        assert session.get(PendingDispatch, dispatch_id).published_at is None
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=next_payload,
    )
    assert len(attempts) == 2 and attempts[0] == attempts[1]
