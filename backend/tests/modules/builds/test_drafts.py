from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.builds.drafts import (
    collect_pages,
    continue_draft,
    create_draft,
    edit_material_groups,
    prepare_draft,
    update_draft,
)
from app.modules.builds.models import (
    BuildDraft,
    DraftAccount,
    DraftDrama,
    DraftGroupMaterial,
    DraftInput,
    DraftPreparation,
)
from app.modules.providers.models import (
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)
from app.modules.providers.schemas import link_reuse_key
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.service import create_strategy
from tests.modules.materials.test_tenant_materials import material
from tests.modules.strategies.test_versions import config


def create_intent(session, context):
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="bc-draft"))
    connection = ProviderConnection(
        tenant_id=context.tenant_id,
        kind="jiashu",
        display_name="test",
        encrypted_credentials="unused",
        status="active",
    )
    session.add(connection)
    session.flush()
    session.add(
        ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            external_id="app-draft",
            name="test",
        )
    )
    strategy = create_strategy(session, context=context, name="test", config=config())
    session.flush()
    version = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
    ).one()
    return {
        "bc_id": "bc-draft",
        "strategy_version_id": version.id,
        "provider_connection_id": connection.id,
        "application_id": "app-draft",
        "drama_lines": [" Moon ", "Moon", "", "Short Drama"],
        "account_lines": ["account-A", "account-A", "missing", ""],
        "link_config": {"episode": 1, "charge_level": 1},
    }


@pytest.fixture
def intent(session, context):
    return create_intent(session, context)


def account(session, context, identity="account-A"):
    connection = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(connection)
    session.add(
        AdvertiserAccount(
            tenant_id=context.tenant_id,
            advertiser_id=identity,
            name=identity,
            currency="USD",
            timezone="UTC",
            remote_status="ENABLE",
        )
    )
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id="bc-draft",
            advertiser_id=identity,
            connection_id=connection.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_build=True,
            permission_state="VERIFIED",
        )
    )
    session.flush()
    # Explicit offline COMPLETE evidence: VERIFIED flags alone no longer establish
    # a current-token role/scope proof. No credentials or remote SDK are needed.
    from app.modules.accounts.capabilities import _directory_basis
    from app.modules.accounts.capability_models import (
        CapabilityAsset,
        CapabilityJob,
        CapabilityPage,
    )

    job = CapabilityJob(
        tenant_id=context.tenant_id,
        bc_id="bc-draft",
        connection_id=connection.id,
        actor_id=context.actor_id,
        credential_revision=connection.credential_revision,
        directory_basis=_directory_basis(session, context, "bc-draft", connection.id),
        status="COMPLETE",
        phase="DONE",
        scope_known=True,
        scope_build=True,
        completed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(job)
    session.flush()
    session.add(
        CapabilityPage(
            job_id=job.id,
            page=1,
            tenant_id=context.tenant_id,
            bc_id="bc-draft",
            row_count=1,
        )
    )
    session.flush()
    session.add(
        CapabilityAsset(
            job_id=job.id,
            page=1,
            tenant_id=context.tenant_id,
            bc_id="bc-draft",
            advertiser_id=identity,
            role="OPERATOR",
        )
    )
    session.flush()


def ready_links(session, context, task_id, intent):
    prep = session.get(DraftPreparation, task_id)
    for item in session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == prep.provider_task_id
        )
    ).all():
        drama = ProviderDrama(
            tenant_id=context.tenant_id,
            connection_id=intent["provider_connection_id"],
            application_id=intent["application_id"],
            external_drama_id=str(item.line_no),
            title=item.raw_input,
        )
        session.add(drama)
        session.flush()
        link = PromotionLink(
            tenant_id=context.tenant_id,
            connection_id=intent["provider_connection_id"],
            application_id=intent["application_id"],
            drama_id=drama.id,
            reuse_key=link_reuse_key(
                context.tenant_id,
                intent["provider_connection_id"],
                intent["application_id"],
                drama.external_drama_id,
                intent["link_config"],
            ),
            config=intent["link_config"],
            url="https://example.com/drama",
            protected_base=item.raw_input,
            status="ready",
            verified_at=datetime.now(UTC),
        )
        session.add(link)
        session.flush()
        item.status = "ready"
        item.resolved = {
            **item.resolved,
            "drama_id": str(drama.id),
            "external_drama_id": drama.external_drama_id,
            "title": drama.title,
            "link_id": str(link.id),
            "url": link.url,
            "protected_base": link.protected_base,
            "status": "ready",
        }
        session.add(item)
    session.flush()


def finish(session, context, task_id):
    for _ in range(100):
        if continue_draft(session, context=context, task_id=task_id):
            return
    raise AssertionError("preparation did not finish")


def test_create_keeps_raw_lines_and_is_idempotent(
    session, context, other_context, intent
):
    request = uuid4()
    draft_id = create_draft(session, context=context, request_id=request, **intent)
    assert (
        create_draft(session, context=context, request_id=request, **intent) == draft_id
    )
    rows = session.exec(
        select(DraftInput)
        .where(DraftInput.draft_id == draft_id)
        .order_by(DraftInput.kind, DraftInput.line_no)
    ).all()
    assert [r.raw_text for r in rows if r.kind == "drama"] == intent["drama_lines"]
    assert [r.status for r in rows if r.kind == "drama"] == [
        "pending",
        "duplicate",
        "empty",
        "pending",
    ]
    with pytest.raises(
        DomainError, check=lambda error: error.code == "idempotency_conflict"
    ):
        create_draft(
            session,
            context=context,
            request_id=request,
            **(intent | {"account_lines": ["different"]}),
        )
    with pytest.raises(DomainError):
        create_draft(session, context=other_context, **intent)


def test_preparation_matches_all_material_pages_once_per_drama(
    session, context, intent
):
    account(session, context)
    expected = [
        material(session, context, f"Moon - {i:03}.mp4", bc="bc-draft")
        for i in range(205)
    ]
    shared = material(session, context, "Moon - Short Drama.mp4", bc="bc-draft")
    material(session, context, "Short.mp4", bc="bc-draft")
    draft_id = create_draft(session, context=context, **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    finish(session, context, task_id)
    rows = session.exec(
        select(DraftGroupMaterial).where(DraftGroupMaterial.draft_id == draft_id)
    ).all()
    assert len(rows) == 207
    assert sum(row.material_id == shared.id for row in rows) == 2
    assert {row.material_id for row in rows} == {m.id for m in [*expected, shared]}
    assert (
        session.exec(select(DraftAccount).where(DraftAccount.draft_id == draft_id))
        .one()
        .advertiser_id
        == "account-A"
    )
    assert session.get(BuildDraft, draft_id).status == "READY"
    moon = session.exec(
        select(DraftDrama).where(
            DraftDrama.draft_id == draft_id, DraftDrama.title == "Moon"
        )
    ).one()
    groups = [row for row in rows if row.drama_id == moon.drama_id]
    assert max(row.group_no for row in groups) == 21
    assert sum(row.group_no == 21 for row in groups) == 6
    revision = edit_material_groups(
        session,
        context=context,
        draft_id=draft_id,
        drama_id=moon.drama_id,
        expected_revision=1,
        groups=[[shared.id]],
    )
    assert revision == 2
    with pytest.raises(
        DomainError, check=lambda error: error.code == "draft_revision_conflict"
    ):
        edit_material_groups(
            session,
            context=context,
            draft_id=draft_id,
            drama_id=moon.drama_id,
            expected_revision=1,
            groups=[],
        )
    assert (
        session.get(
            DraftDrama, (context.tenant_id, draft_id, moon.drama_id)
        ).material_state
        == "manual"
    )


def test_unknown_link_keeps_original_status_and_input(session, context, intent):
    account(session, context)
    draft_id = create_draft(session, context=context, **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    prep = session.get(DraftPreparation, task_id)
    for item in session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == prep.provider_task_id
        )
    ).all():
        item.status = "result_unknown"
        item.resolved = {
            **item.resolved,
            "status": "result_unknown",
            "error_code": "provider_result_unknown",
        }
        session.add(item)
    session.flush()
    finish(session, context, task_id)
    row = session.exec(
        select(DraftInput).where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "drama",
            DraftInput.line_no == 1,
        )
    ).one()
    assert row.status == "result_unknown" and row.raw_text == " Moon "
    assert session.get(BuildDraft, draft_id).status == "READY"


def test_collect_pages_preserves_every_material_and_rejects_repeated_cursor():
    first = SimpleNamespace(
        material_id=UUID(int=1), file_name="Long Drama - Short Drama.mp4"
    )
    second = SimpleNamespace(material_id=UUID(int=2), file_name="Short Drama - 02.mp4")
    pages = {
        None: Page(items=[first], next_cursor="page2"),
        "page2": Page(items=[second]),
    }
    assert list(collect_pages(lambda cursor: pages[cursor])) == [first, second]
    with pytest.raises(
        DomainError, check=lambda error: error.code == "repeated_cursor"
    ):
        list(collect_pages(lambda cursor: Page(items=[first], next_cursor="again")))


def test_input_update_obsoletes_old_job_and_preserves_new_raw_input(
    session, context, intent
):
    draft_id = create_draft(session, context=context, **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert (
        update_draft(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=1,
            drama_lines=[" New Story ", ""],
        )
        == 2
    )
    assert continue_draft(session, context=context, task_id=task_id)
    assert session.get(DraftPreparation, task_id).status == "OBSOLETE"
    assert session.get(BuildDraft, draft_id).status == "DRAFT"
    assert session.exec(
        select(DraftInput.raw_text)
        .where(DraftInput.draft_id == draft_id, DraftInput.kind == "drama")
        .order_by(DraftInput.line_no)
    ).all() == [" New Story ", ""]
    with pytest.raises(
        DomainError, check=lambda error: error.code == "draft_revision_conflict"
    ):
        update_draft(
            session,
            context=context,
            draft_id=draft_id,
            expected_revision=1,
            account_lines=[],
        )


def test_account_dedup_spans_processing_pages(session, context, intent):
    account(session, context)
    values = ["account-A"] + ["missing"] * 99 + ["account-A", "account-A"]
    draft_id = create_draft(
        session, context=context, **(intent | {"account_lines": values})
    )
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    finish(session, context, task_id)
    rows = session.exec(
        select(DraftInput)
        .where(
            DraftInput.draft_id == draft_id,
            DraftInput.kind == "account",
            DraftInput.line_no >= 101,
        )
        .order_by(DraftInput.line_no)
    ).all()
    assert [(row.status, row.duplicate_of) for row in rows] == [
        ("duplicate", 1),
        ("duplicate", 1),
    ]


def test_manual_materials_reject_cross_bc_and_duplicate_without_revision_bump(
    session, context, intent
):
    draft_id = create_draft(session, context=context, **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    finish(session, context, task_id)
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == draft_id)
    ).first()
    foreign = material(session, context, "Moon.mp4", bc="bc-other")
    for group in ([[foreign.id]], [[foreign.id, foreign.id]]):
        with pytest.raises(DomainError):
            edit_material_groups(
                session,
                context=context,
                draft_id=draft_id,
                drama_id=drama.drama_id,
                expected_revision=1,
                groups=group,
            )
        assert session.get(BuildDraft, draft_id).revision == 1


def test_prepare_request_aliases_stay_bound_and_refresh_observes_same_provider_task(
    session, context, intent
):
    draft_id = create_draft(session, context=context, **intent)
    first = uuid4()
    task = prepare_draft(session, context=context, draft_id=draft_id, request_id=first)
    alias = uuid4()
    assert (
        prepare_draft(session, context=context, draft_id=draft_id, request_id=alias)
        == task
    )
    second_draft = create_draft(session, context=context, **intent)
    with pytest.raises(
        DomainError, check=lambda error: error.code == "idempotency_conflict"
    ):
        prepare_draft(session, context=context, draft_id=second_draft, request_id=alias)
    ready_links(session, context, task, intent)
    finish(session, context, task)
    new_task = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    assert new_task != task
    assert (
        session.get(DraftPreparation, new_task).provider_task_id
        == session.get(DraftPreparation, task).provider_task_id
    )
    assert session.get(BuildDraft, draft_id).revision == 2
    assert (
        prepare_draft(session, context=context, draft_id=draft_id, request_id=first)
        == task
    )


def test_scene_reference_uses_a_current_ready_link_when_first_drama_link_changed(
    session, context, intent, monkeypatch
):
    from app.modules.builds import drafts
    from app.modules.builds.scene_schemas import ScenePreparation

    account(session, context)
    material(session, context, "Moon - Short Drama.mp4", bc="bc-draft")
    draft_id = create_draft(session, context=context, **intent)
    task_id = prepare_draft(
        session, context=context, draft_id=draft_id, request_id=uuid4()
    )
    ready_links(session, context, task_id, intent)
    prep = session.get(DraftPreparation, task_id)
    for _ in range(10):
        if prep.phase == "materials":
            break
        continue_draft(session, context=context, task_id=task_id)
    assert prep.phase == "materials"
    dramas = session.exec(
        select(DraftDrama)
        .where(DraftDrama.draft_id == draft_id)
        .order_by(DraftDrama.first_line)
    ).all()
    assert len(dramas) == 2
    session.get(PromotionLink, dramas[0].link_id).status = "invalid"
    session.flush()
    called = []

    def ensure(_session, **kwargs):
        called.append(kwargs["link_id"])
        return ScenePreparation(None, "blocked", "fixture_scene_unavailable")

    monkeypatch.setattr(drafts, "ensure_scene_preparation", ensure)
    finish(session, context, task_id)
    assert called == [dramas[1].link_id]
