"""Independent regression probes for frozen-preview failure isolation."""

import pytest
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


def test_invalidated_link_blocks_only_its_drama(
    session, context, prepared, monkeypatch
):
    from app.modules.builds.models import DraftDrama
    from app.modules.providers.models import PromotionLink

    valid_scene = previews.read_scene_context
    drama = session.exec(
        select(DraftDrama).where(DraftDrama.draft_id == prepared)
    ).first()
    session.get(PromotionLink, drama.link_id).status = "failed"
    session.flush()

    def scene(session, **kwargs):
        return (
            read_scene_context(session, **kwargs)
            if kwargs["link_id"] == drama.link_id
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
    assert summary.blocked_count == 3 and summary.campaign_count == 3
    assert summary.daily_budget_sum == 300


@pytest.mark.parametrize(
    "revocation,code",
    [("role", "action_forbidden"), ("membership", "tenant_forbidden")],
)
def test_actor_revocation_still_fails_entire_worker_before_expansion(
    session, context, prepared, revocation, code
):
    from app.modules.builds.preview_models import BuildPreview, BuildUnit
    from app.modules.builds.preview_tasks import process_preview
    from app.modules.tenants.models import TenantMembership

    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    if revocation == "role":
        member.role = "viewer"
    else:
        member.active = False
    session.flush()
    process_preview(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preview_id": str(identity), "generation": 0},
    )
    session.expire_all()
    header = session.get(BuildPreview, identity)
    assert header.status == "FAILED" and header.error_code == code
    assert (
        session.exec(select(BuildUnit).where(BuildUnit.preview_id == identity)).all()
        == []
    )


def test_eligible_unit_does_not_freeze_revoked_connection_when_account_has_new_grant(
    session, context, prepared, monkeypatch
):
    from app.modules.accounts.access import resolve_account_access
    from app.modules.accounts.models import TikTokConnection
    from app.modules.builds.models import DraftAccount

    original_scene = previews.read_scene_context
    draft_account = session.get(DraftAccount, (context.tenant_id, prepared, "B"))
    old_id = draft_account.connection_id
    old_grant = session.get(
        BCAccountAccess, (context.tenant_id, "bc-draft", "B", old_id)
    )
    old_grant.authorized = False
    new = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(new)
    session.flush()
    session.add(
        BCAccountAccess(
            tenant_id=context.tenant_id,
            bc_id="bc-draft",
            advertiser_id="B",
            connection_id=new.id,
            in_bc=True,
            authorized=True,
            active=True,
            can_build=True,
            permission_state="VERIFIED",
        )
    )
    session.flush()

    def current_scene(session, **kwargs):
        # Scene's actual public contract resolves current advertiser access; it
        # does not expose or pin the old DraftAccount connection ID.
        access = resolve_account_access(
            session,
            context=kwargs["context"],
            bc_id=kwargs["bc_id"],
            advertiser_id=kwargs["advertiser_id"],
            action="build",
        )
        if kwargs["advertiser_id"] == "B":
            assert access.connection_id == new.id
        return original_scene(session, **kwargs)

    monkeypatch.setattr(previews, "read_scene_context", current_scene)
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    for unit in previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items:
        if unit.advertiser_id == "B" and unit.readiness != "BLOCKED":
            frozen = previews.load_frozen_unit(
                session, context=context, unit_id=unit.unit_id
            )
            assert frozen.connection_id == new.id, (
                "Eligible frozen unit retains revoked connection"
            )
