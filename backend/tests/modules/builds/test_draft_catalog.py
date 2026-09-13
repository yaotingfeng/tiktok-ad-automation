"""真实 PostgreSQL 验证未完成搭建的范围、分页、恢复目标和只读行为。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.accounts.models import TenantBC
from app.modules.accounts.resolver import encode_cursor
from app.modules.builds.draft_catalog import list_drafts
from app.modules.builds.drafts import create_draft, update_draft
from app.modules.builds.execution_models import Submission
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.preview_models import BuildPreview
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_drafts import create_intent


def preview(session, context, draft_id, intent, *, revision=1, status="BUILDING"):
    value = BuildPreview(
        tenant_id=context.tenant_id,
        bc_id=intent["bc_id"],
        draft_id=draft_id,
        draft_revision=revision,
        strategy_version_id=intent["strategy_version_id"],
        actor_id=context.actor_id,
        batch_short_id=uuid4().hex,
        local_date="20260913",
        status=status,
        content_digest="synthetic-catalog-fixture" if status == "FROZEN" else None,
        config={},
        budget=100,
        target_roas=1,
    )
    session.add(value)
    session.flush()
    return value


def test_saved_and_preparing_drafts_keep_input_summary_without_starting_jobs(
    session, context, intent
):
    intent = intent | {
        "drama_lines": [*intent["drama_lines"], " \t "],
        "account_lines": [*intent["account_lines"], "\t"],
    }
    ids = []
    for status in ("DRAFT", "PREPARING", "READY", "BLOCKED"):
        identity = create_draft(session, context=context, **intent)
        row = session.get(BuildDraft, identity)
        row.status = status
        session.add(row)
        ids.append(identity)
    session.flush()
    before = session.exec(select(func.count()).select_from(DraftPreparation)).one()
    result = list_drafts(session, context=context, bc_id=intent["bc_id"])
    assert {row.draft_id for row in result.items} == set(ids)
    assert {row.status for row in result.items} == {
        "DRAFT",
        "PREPARING",
        "READY",
        "BLOCKED",
    }
    for row in result.items:
        assert row.drama_titles == [" Moon ", "Short Drama"]
        # 明确统计非空原输入行，不能把未解析或重复输入宣称为有效账户。
        assert (row.drama_input_count, row.account_input_count) == (3, 3)
        assert row.resolved_account_count == 0
        assert row.strategy_label == "test v1"
        assert row.preview_id is None
    assert (
        session.exec(select(func.count()).select_from(DraftPreparation)).one() == before
    )


def test_catalog_is_tenant_and_bc_scoped(session, context, other_context, intent):
    mine = create_draft(session, context=context, **intent)
    other_intent = create_intent(session, other_context)
    create_draft(session, context=other_context, **other_intent)
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="another-bc"))
    session.flush()
    create_draft(session, context=context, **(intent | {"bc_id": "another-bc"}))
    assert [
        r.draft_id
        for r in list_drafts(session, context=context, bc_id="bc-draft").items
    ] == [mine]
    with pytest.raises(DomainError, match="BC"):
        list_drafts(session, context=context, bc_id="missing-bc")


def test_pagination_ties_and_recent_edits(session, context, intent):
    stamp = datetime(2026, 9, 13, tzinfo=UTC)
    ids = []
    for _ in range(5):
        identity = create_draft(session, context=context, **intent)
        row = session.get(BuildDraft, identity)
        row.updated_at = stamp
        session.add(row)
        ids.append(identity)
    session.flush()
    found, cursor = [], None
    while True:
        page = list_drafts(
            session, context=context, bc_id="bc-draft", limit=2, cursor=cursor
        )
        found.extend(r.draft_id for r in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert found == sorted(ids, reverse=True)
    oldest = session.get(BuildDraft, found[-1])
    oldest.updated_at = stamp + timedelta(hours=1)
    session.add(oldest)
    session.flush()
    assert (
        list_drafts(session, context=context, bc_id="bc-draft", limit=1)
        .items[0]
        .draft_id
        == oldest.id
    )


def test_cursor_rejects_other_scopes_and_malformed_positions(session, context, intent):
    for _ in range(2):
        create_draft(session, context=context, **intent)
    cursor = list_drafts(
        session, context=context, bc_id="bc-draft", limit=1
    ).next_cursor
    session.add(TenantBC(tenant_id=context.tenant_id, bc_id="another-bc"))
    session.flush()
    with pytest.raises(DomainError):
        list_drafts(session, context=context, bc_id="another-bc", cursor=cursor)
    bad = encode_cursor(
        scope={
            "tenant": str(context.tenant_id),
            "bc": "bc-draft",
            "kind": "unfinished-drafts",
        },
        last_id="invalid|uuid",
    )
    with pytest.raises(DomainError):
        list_drafts(session, context=context, bc_id="bc-draft", cursor=bad)
    with pytest.raises(DomainError):
        list_drafts(session, context=context, bc_id="bc-draft", limit=101)


def test_resume_only_current_preview_and_hide_submitted_batches(
    session, context, intent
):
    current = create_draft(session, context=context, **intent)
    original = preview(session, context, current, intent)
    assert (
        list_drafts(session, context=context, bc_id="bc-draft").items[0].preview_id
        == original.id
    )
    update_draft(
        session,
        context=context,
        draft_id=current,
        expected_revision=1,
        drama_lines=["New drama"],
    )
    assert (
        list_drafts(session, context=context, bc_id="bc-draft").items[0].preview_id
        is None
    )
    latest = preview(session, context, current, intent, revision=2, status="FROZEN")
    session.add(
        Submission(
            tenant_id=context.tenant_id,
            bc_id="bc-draft",
            draft_id=current,
            preview_id=latest.id,
            actor_id=context.actor_id,
            ordinal=1,
        )
    )
    session.flush()
    assert list_drafts(session, context=context, bc_id="bc-draft").items == []


def test_titles_are_bounded_and_counts_cover_all_input_rows(session, context, intent):
    create_draft(
        session,
        context=context,
        **(
            intent
            | {
                "drama_lines": [f"Drama {i}" for i in range(205)],
                "account_lines": [f"Account {i}" for i in range(2001)],
            }
        ),
    )
    row = list_drafts(session, context=context, bc_id="bc-draft").items[0]
    assert row.drama_titles == ["Drama 0", "Drama 1", "Drama 2"]
    assert (row.drama_input_count, row.account_input_count) == (205, 2001)


def test_http_viewer_can_list_but_cross_tenant_is_denied(
    client, session, context, other_context, intent
):
    from tests.modules.strategies.test_api import headers

    create_draft(session, context=context, **intent)
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add(member)
    session.flush()
    base = f"/api/tenants/{context.tenant_id}/build-drafts"
    response = client.get(base, params={"bc_id": "bc-draft"}, headers=headers(context))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert client.get(base, headers=headers(context)).status_code == 422
    assert (
        client.get(
            base, params={"bc_id": "bc-draft", "limit": 101}, headers=headers(context)
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/build-drafts",
            params={"bc_id": "bc-draft"},
            headers=headers(context),
        ).status_code
        == 403
    )
