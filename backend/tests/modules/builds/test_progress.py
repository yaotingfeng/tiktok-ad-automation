from uuid import uuid4

import pytest
from sqlmodel import select

from app.modules.builds import submissions
from app.modules.builds.execution_models import ExecutionStep
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submissions import frozen as frozen


def assert_conservation(view):
    for field in ("campaign_count", "adgroup_count", "ad_count"):
        assert getattr(view.planned, field) == getattr(view.submitted, field) + getattr(
            view.excluded, field
        )
        assert getattr(view.submitted, field) == sum(
            getattr(getattr(view, key), field)
            for key in ("succeeded", "failed", "unknown", "pending")
        )


def test_unknown_precedes_failure():
    assert (
        submissions.aggregate_status(["SUCCEEDED", "FAILED", "UNKNOWN"])
        == "NEEDS_REVIEW"
    )
    assert submissions.aggregate_status(["SUCCEEDED", "FAILED"]) == "PARTIAL"
    assert submissions.aggregate_status(["SUCCEEDED"]) == "COMPLETED"


def test_summary_before_and_after_expansion_counts_frozen_objects(
    session, context, frozen
):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    first = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(first)
    assert first.status == "QUEUED" and first.submitted.model_dump() == {
        "campaign_count": 6,
        "adgroup_count": 18,
        "ad_count": 36,
    }
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    next_ = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(next_)
    assert (
        next_.pending == first.pending
        and next_.daily_budget_sum == first.daily_budget_sum
    )
    campaign = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    campaign.status = "FAILED"
    session.add(campaign)
    session.flush()
    failed = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(failed)
    assert failed.failed.model_dump() == {
        "campaign_count": 1,
        "adgroup_count": 3,
        "ad_count": 6,
    }
    ad = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "AD", ExecutionStep.unit_id != campaign.unit_id
        )
    ).first()
    ad.status = "UNKNOWN"
    session.add(ad)
    session.flush()
    unknown = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(unknown)
    assert unknown.status == "NEEDS_REVIEW" and unknown.unknown.ad_count == 1
    ad.remote_id = "actual-ad"
    ad.mismatch = True
    session.add(ad)
    session.flush()
    mismatch = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(mismatch)
    assert (
        mismatch.status == "NEEDS_REVIEW"
        and mismatch.succeeded.ad_count == 1
        and mismatch.unknown.ad_count == 0
    )


def test_readonly_sql_pages_and_cursor_scope(session, context, other_context, frozen):
    from sqlalchemy import event

    from app.core.errors import DomainError

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    session.flush()
    seen = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        assert statement.lstrip().upper().startswith(("SELECT", "WITH"))
        assert "FOR UPDATE" not in statement.upper()
        seen.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", observe)
    try:
        view = submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        )
        assert_conservation(view)
        page = submissions.get_submission_steps(
            session, context=context, submission_id=receipt.submission_id
        )
        assert len(page.items) == 50 and page.next_cursor
        with pytest.raises(DomainError):
            submissions.get_submission_steps(
                session,
                context=context,
                submission_id=receipt.submission_id,
                cursor=page.next_cursor,
                kind="AD",
            )
        with pytest.raises(DomainError):
            submissions.get_submission_steps(
                session,
                context=other_context,
                submission_id=receipt.submission_id,
                cursor=page.next_cursor,
            )
        units = submissions.get_submission_units(
            session, context=context, submission_id=receipt.submission_id, limit=2
        )
        assert len(units.items) == 2 and units.next_cursor
    finally:
        event.remove(connection, "before_cursor_execute", observe)
    assert seen


def test_success_without_remote_id_is_not_a_created_object(session, context, frozen):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    step = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    step.status = "SUCCEEDED"
    session.add(step)
    session.flush()
    view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(view)
    assert (
        view.succeeded.campaign_count == 0
        and view.unknown.campaign_count == 1
        and view.status == "NEEDS_REVIEW"
    )


def test_all_failed_campaigns_include_descendants_and_finish_failed(
    session, context, frozen
):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    for step in session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).all():
        step.status = "FAILED"
        session.add(step)
    session.flush()
    view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(view)
    assert (
        view.status == "FAILED"
        and view.failed == view.submitted
        and view.pending.ad_count == 0
    )


def test_material_failure_blocks_only_its_group_and_other_groups_can_build(
    session, context, frozen
):
    from app.modules.builds.preview_models import PlannedGroup, PreviewGroupMaterial

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    campaign = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    groups = session.exec(
        select(PlannedGroup)
        .where(PlannedGroup.unit_id == campaign.unit_id)
        .order_by(PlannedGroup.group_no)
    ).all()
    failed_material = session.exec(
        select(PreviewGroupMaterial.material_id).where(
            PreviewGroupMaterial.preview_id == frozen,
            PreviewGroupMaterial.drama_id == groups[0].drama_id,
            PreviewGroupMaterial.group_no == groups[0].group_no,
        )
    ).first()
    for step in session.exec(
        select(ExecutionStep).where(
            ExecutionStep.unit_id == campaign.unit_id,
            ExecutionStep.kind.in_(["MATERIAL", "CTA"]),
        )
    ).all():
        step.status = "FAILED" if step.material_id == failed_material else "SUCCEEDED"
        session.add(step)
    session.flush()
    claim = submissions.claim_step(
        session, context=context, step_id=campaign.id, owner=uuid4()
    )
    assert claim is not None
    campaign.status = "SUCCEEDED"
    campaign.remote_id = "created-campaign"
    session.add(campaign)
    session.flush()
    for index, group in enumerate(groups):
        step = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.group_id == group.id, ExecutionStep.kind == "ADGROUP"
            )
        ).one()
        claim = submissions.claim_step(
            session, context=context, step_id=step.id, owner=uuid4()
        )
        assert (claim is None) == (index == 0)
    view = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert_conservation(view)
    assert view.failed.model_dump() == {
        "campaign_count": 0,
        "adgroup_count": 1,
        "ad_count": 2,
    }
    assert view.succeeded.campaign_count == 1
