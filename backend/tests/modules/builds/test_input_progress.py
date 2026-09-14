"""剧目输入在取链前后保持同一分页身份，读取不会触发任务。"""

from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds import catalog
from app.modules.builds.drafts import create_draft, prepare_draft, update_draft
from app.modules.builds.models import DraftPreparation
from app.modules.providers.models import LinkPreparationItem
from tests.modules.builds.test_drafts import account, finish, ready_links


def test_summary_reports_current_preparation_phase_without_advancing_work(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    assert (
        catalog.draft_summary(
            session, context=context, draft_id=draft
        ).preparation_phase
        is None
    )
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    prep = session.get(DraftPreparation, task)
    generation = prep.generation
    ready_links(session, context, task, intent)
    summary = catalog.draft_summary(session, context=context, draft_id=draft)
    assert summary.preparation_phase == "accounts"
    assert summary.status == "PREPARING"
    assert summary.drama_count == 0
    assert prep.generation == generation
    finish(session, context, task)
    assert (
        catalog.draft_summary(
            session, context=context, draft_id=draft
        ).preparation_phase
        == "done"
    )
    update_draft(
        session,
        context=context,
        draft_id=draft,
        expected_revision=summary.revision,
        drama_lines=["Changed"],
    )
    assert (
        catalog.draft_summary(
            session, context=context, draft_id=draft
        ).preparation_phase
        is None
    )


def test_wangyan_numeric_display_and_edit_preserve_opaque_identity(
    session, context, intent
):
    from app.modules.builds.manual_links import save_manual_link
    from app.modules.builds.models import DraftDrama
    from app.modules.providers.models import (
        PromotionLink,
        ProviderConnection,
        ProviderDrama,
    )

    account(session, context)
    intent["drama_lines"] = ["Moon"]
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    finish(session, context, task)
    dd = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).one()
    original_id = dd.drama_id
    drama = session.get(ProviderDrama, original_id)
    connection = session.get(ProviderConnection, drama.connection_id)
    connection.kind = "wangyan"
    drama.external_drama_id = "6a98f85eadb6903f924e6950"
    old_link = session.get(PromotionLink, dd.link_id)
    old_link.attribution = {"drama_int_id": 31091}
    old_link.url = "https://www.tiktok.com/minis/original?x=a%2Bb&x=2"
    session.flush()
    row = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama"
    ).items[0]
    assert row.preparation.display_drama_id == "31091"
    assert row.preparation.url == old_link.url
    assert row.preparation.protected_base == old_link.protected_base
    # 模拟迁移/新解析已补齐的展示编号，手动修改仍绑定原技术身份。
    drama.display_drama_id = "31091"
    session.flush()
    revision = catalog.draft_summary(session, context=context, draft_id=draft).revision
    save_manual_link(
        session,
        context=context,
        draft_id=draft,
        input_id=row.id,
        expected_revision=revision,
        request_id=uuid4(),
        link={
            "line_no": 1,
            "url": "https://www.tiktok.com/minis/changed",
            "external_drama_id": "31091",
            "protected_base": "new-attribution",
        },
    )
    prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    changed = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).one()
    assert changed.drama_id == original_id
    assert drama.external_drama_id == "6a98f85eadb6903f924e6950"
    assert session.get(PromotionLink, changed.link_id).source == "manual"
    assert session.get(PromotionLink, changed.link_id).url.endswith("/changed")
    assert old_link.url.endswith("?x=a%2Bb&x=2")


def test_inputs_visible_before_preparation_and_link_status_before_draft_sync(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    page = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama", limit=2
    )
    assert [row.raw_text for row in page.items] == [" Moon ", "Moon"]
    assert all(row.preparation.drama is None for row in page.items)
    identities = [row.id for row in page.items]
    cursor = page.next_cursor
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    prep = session.get(DraftPreparation, task)
    link = session.exec(
        select(LinkPreparationItem).where(
            LinkPreparationItem.preparation_id == prep.provider_task_id,
            LinkPreparationItem.line_no == 1,
        )
    ).one()
    link.resolved = {
        **link.resolved,
        "title": "Moon",
        "external_drama_id": "provider-123",
        "error_code": None,
        "candidates": [{"title": "Moon", "external_drama_id": "provider-123"}],
    }
    link.status = "needs_resolution"
    session.flush()
    candidate = (
        catalog.inputs_page(session, context=context, draft_id=draft, kind="drama")
        .items[0]
        .preparation
    )
    assert candidate.provider_input_id == link.id
    assert candidate.candidates[0].external_drama_id == "provider-123"
    link.status = "creating"
    session.flush()
    page = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama", limit=2
    )
    assert [row.id for row in page.items] == identities
    assert page.items[0].preparation.title == "Moon"
    assert page.items[0].preparation.external_drama_id == "provider-123"
    assert page.items[0].preparation.candidates == []
    assert page.items[0].preparation.link_status == "creating"
    assert page.items[0].preparation.drama is None
    assert page.next_cursor == cursor
    link.status = "result_unknown"
    link.resolved = {**link.resolved, "error_code": "provider_result_unknown"}
    session.flush()
    result = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama"
    ).items[0]
    assert result.preparation.link_status == "result_unknown"
    assert result.preparation.reason_code == "provider_result_unknown"


def test_ready_and_duplicate_inputs_remain_in_order_and_new_input_clears_progress(
    session, context, intent
):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    finish(session, context, task)
    page = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama", limit=2
    )
    assert [row.line_no for row in page.items] == [1, 2]
    assert page.items[0].preparation.drama.title == "Moon"
    assert page.items[0].preparation.external_drama_id == "1"
    assert page.items[0].preparation.candidates == []
    assert page.items[1].preparation.link_status == "duplicate"
    assert page.items[1].preparation.drama is None
    assert page.items[1].duplicate_of == 1
    tail = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama", cursor=page.next_cursor
    )
    assert [row.line_no for row in tail.items] == [3, 4]
    assert tail.items[0].preparation.link_status == "empty"
    revision = catalog.draft_summary(session, context=context, draft_id=draft).revision
    update_draft(
        session,
        context=context,
        draft_id=draft,
        expected_revision=revision,
        drama_lines=["New drama"],
    )
    updated = catalog.inputs_page(
        session, context=context, draft_id=draft, kind="drama"
    )
    assert len(updated.items) == 1
    assert updated.items[0].raw_text == "New drama"
    assert updated.items[0].preparation.title is None
    assert updated.items[0].preparation.external_drama_id is None
    assert updated.items[0].preparation.candidates == []
    assert updated.items[0].preparation.drama is None
    with pytest.raises(DomainError, match="分页|游标"):
        catalog.inputs_page(
            session,
            context=context,
            draft_id=draft,
            kind="drama",
            cursor=page.next_cursor,
        )
