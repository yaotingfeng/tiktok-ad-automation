"""搭建目标 BC 和租户素材来源独立；手动选材按可信内容防重复。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.modules.builds.catalog import materials_page
from app.modules.builds.drafts import create_draft, edit_material_groups, prepare_draft
from app.modules.builds.models import BuildDraft, DraftDrama
from tests.modules.builds.test_drafts import account, create_intent, finish, ready_links
from tests.modules.materials.test_tenant_materials import material


def prepared(session, context):
    intent = create_intent(session, context)
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    ready_links(session, context, task, intent)
    finish(session, context, task)
    drama = session.exec(select(DraftDrama).where(DraftDrama.draft_id == draft)).first()
    return draft, drama.drama_id


def test_manual_cross_bc_material_is_visible_with_origin(session, context):
    draft, drama = prepared(session, context)
    source = material(session, context, "From A.mp4", bc="bc-a")
    revision = session.get(BuildDraft, draft).revision
    edit_material_groups(
        session,
        context=context,
        draft_id=draft,
        drama_id=drama,
        expected_revision=revision,
        groups=[[source.id]],
    )
    page = materials_page(session, context=context, draft_id=draft, drama_id=drama)
    assert [(item.material_id, item.source_bc_id) for item in page.items] == [
        (source.id, "bc-a")
    ]
    assert page.items[0].content_key == f"material:{source.id}"
    assert session.get(BuildDraft, draft).bc_id == "bc-draft"


def test_manual_distinct_ids_same_verified_content_rejected(session, context):
    draft, drama = prepared(session, context)
    a = material(session, context, "First.mp4", bc="bc-draft")
    b = material(session, context, "Second.mp4", bc="bc-draft")
    for row in (a, b):
        row.sha256, row.video_md5 = "a" * 64, "b" * 32
        row.digest_verified_at = datetime.now(UTC)
        session.add(row)
    session.flush()
    revision = session.get(BuildDraft, draft).revision
    with pytest.raises(
        DomainError, check=lambda error: error.code == "draft_groups_invalid"
    ):
        edit_material_groups(
            session,
            context=context,
            draft_id=draft,
            drama_id=drama,
            expected_revision=revision,
            groups=[[a.id], [b.id]],
        )
    assert session.get(BuildDraft, draft).revision == revision


def test_matching_continuation_does_not_repeat_content_when_representative_changes(
    session, context
):
    from app.modules.builds.drafts import _materials_page
    from app.modules.builds.models import DraftGroupMaterial, DraftPreparation

    a = material(session, context, "Moon-a.mp4", bc="bc-a")
    a.sha256, a.video_md5 = "a" * 64, "b" * 32
    a.digest_verified_at = datetime.now(UTC)
    session.add(a)
    session.flush()
    draft_id, _ = prepared(session, context)
    drama = session.exec(
        select(DraftDrama).where(
            DraftDrama.draft_id == draft_id, DraftDrama.title == "Moon"
        )
    ).one()
    assert drama.matched_count == 1
    # 分页过程中原代表不可见，另一份同内容上传变成候选代表。
    a.storage_state = "unavailable"
    b = material(session, context, "Moon-b.mp4", bc="bc-a")
    b.sha256, b.video_md5, b.digest_verified_at = (
        a.sha256,
        a.video_md5,
        a.digest_verified_at,
    )
    drama.material_state = "matching"
    session.add_all([a, b, drama])
    session.flush()
    draft = session.get(BuildDraft, draft_id)
    prep = session.exec(
        select(DraftPreparation).where(DraftPreparation.draft_id == draft_id)
    ).one()
    _materials_page(session, context, draft, prep)
    session.flush()
    chosen = session.exec(
        select(DraftGroupMaterial).where(
            DraftGroupMaterial.draft_id == draft_id,
            DraftGroupMaterial.drama_id == drama.drama_id,
        )
    ).all()
    assert [row.material_id for row in chosen] == [a.id]
    assert drama.matched_count == 1
