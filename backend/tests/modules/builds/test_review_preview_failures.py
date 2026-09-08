"""Independent regression probes for frozen-preview failure isolation."""

from sqlmodel import select

from app.modules.accounts.models import BCAccountAccess
from app.modules.builds import previews
from app.modules.builds.scene import read_scene_context
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


def test_account_revoked_after_draft_blocks_its_pairs_without_aborting_preview(
    session, context, prepared, monkeypatch
):
    valid_scene = previews.read_scene_context
    for grant in session.exec(
        select(BCAccountAccess).where(
            BCAccountAccess.tenant_id == context.tenant_id,
            BCAccountAccess.bc_id == "bc-draft",
            BCAccountAccess.advertiser_id == "B",
        )
    ).all():
        grant.authorized = False
    session.flush()

    def scene(session, **kwargs):
        # Exercise the actual access check for the revoked account; remaining
        # accounts keep the already verified local fixture scene facts.
        return (
            read_scene_context(session, **kwargs)
            if kwargs["advertiser_id"] == "B"
            else valid_scene(session, **kwargs)
        )

    monkeypatch.setattr(previews, "read_scene_context", scene)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.status == "FROZEN" and summary.total_unit_count == 6
    assert summary.blocked_count == 2 and summary.campaign_count == 4
    assert summary.daily_budget_sum == 400
