"""第二步保存投放排期：沿用已准备结果，旧预览失效。"""

from uuid import uuid4

import pytest

from tests.modules.builds.test_previews import prepared as prepared


def test_delivery_save_preserves_groups_and_obsoletes_preview(
    session, context, prepared
):
    from sqlmodel import select

    from app.modules.builds import previews
    from app.modules.builds.delivery_service import save_delivery
    from app.modules.builds.models import BuildDraft, DraftGroupMaterial
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.builds.schemas import DeliveryChange
    from tests.modules.builds.test_previews import drain

    draft = session.get(BuildDraft, prepared)
    before = [
        r.model_dump()
        for r in session.exec(
            select(DraftGroupMaterial).where(DraftGroupMaterial.draft_id == prepared)
        ).all()
    ]
    assert before
    preview_id = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=draft.revision
    )
    drain(session, context, preview_id)
    old_revision = draft.revision
    schedule = {
        "schedule_mode": "START_END",
        "schedule_start_time": "2026-11-01 00:00:00",
        "schedule_end_time": "2026-11-08 00:00:00",
    }
    revision = save_delivery(
        session,
        context=context,
        draft_id=prepared,
        body=DeliveryChange(
            request_id=uuid4(),
            expected_revision=old_revision,
            delivery_config=schedule,
        ),
    )
    session.refresh(draft)
    assert revision == old_revision + 1
    assert draft.delivery_config == schedule
    after = [
        r.model_dump()
        for r in session.exec(
            select(DraftGroupMaterial).where(DraftGroupMaterial.draft_id == prepared)
        ).all()
    ]
    assert before == after
    preview = session.get(BuildPreview, preview_id)
    session.refresh(preview)
    assert preview.status == "OBSOLETE"


def test_delivery_save_requires_ready_draft(session, context, prepared):
    from app.core.errors import DomainError
    from app.modules.builds.delivery_service import save_delivery
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.schemas import DeliveryChange

    draft = session.get(BuildDraft, prepared)
    draft.status = "PREPARING"
    session.add(draft)
    session.flush()
    with pytest.raises(DomainError) as error:
        save_delivery(
            session,
            context=context,
            draft_id=prepared,
            body=DeliveryChange(
                request_id=uuid4(),
                expected_revision=draft.revision,
                delivery_config={"schedule_mode": "IMMEDIATE"},
            ),
        )
    assert error.value.code == "draft_not_ready"
