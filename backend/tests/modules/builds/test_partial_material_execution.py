"""排除素材必须在预览、提交、恢复和最后广告请求中保持相同账户范围。"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from app.modules.builds import (
    execution,
    previews,
    recovery,
    submission_catalog,
    submissions,
)
from app.modules.builds.cover_execution import validate_ad_assets
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.execution_schemas import StepClaim
from app.modules.builds.models import DraftGroupMaterial
from app.modules.builds.preview_models import BuildUnit
from app.modules.builds.routes import load_preview_route
from app.modules.materials.models import AccountMaterial
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


@pytest.fixture
def partial(session, context, prepared, monkeypatch):
    rows = session.exec(
        select(DraftGroupMaterial)
        .where(
            DraftGroupMaterial.draft_id == prepared,
        )
        .order_by(
            col(DraftGroupMaterial.drama_id),
            col(DraftGroupMaterial.group_no),
            col(DraftGroupMaterial.position),
        )
    ).all()
    drama = rows[0].drama_id
    ordered = [r.material_id for r in rows if r.drama_id == drama]
    skipped = set(ordered[:11])
    read_scene = previews.read_scene_context
    monkeypatch.setattr(
        previews,
        "read_scene_context",
        lambda *a, **kw: replace(
            read_scene(*a, **kw),
            creative_fields={
                "creative_info": {
                    "identity_type": "BC_AUTH_TT",
                    "identity_id": "identity-1",
                    "identity_authorized_bc_id": "bc-draft",
                    "ad_format": "SINGLE_VIDEO",
                }
            },
        ),
    )
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(
                state="blocked"
                if kw["advertiser_id"] == "A" and identity in skipped
                else "preparable",
                reason_code="material_remote_source_unavailable"
                if kw["advertiser_id"] == "A" and identity in skipped
                else None,
            )
            for identity in kw["material_ids"]
        },
    )
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    receipt = submissions.submit_preview(
        session, context=context, preview_id=preview, request_id=uuid4()
    )
    for _ in range(100):
        if submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            break
    else:
        pytest.fail("提交展开未完成")
    units = previews.get_preview_units(
        session, context=context, preview_id=preview, drama_id=drama
    ).items
    unit = session.get(
        BuildUnit, next(u.unit_id for u in units if u.advertiser_id == "A")
    )
    return SimpleNamespace(
        preview=preview,
        submission=receipt.submission_id,
        unit=unit,
        skipped=skipped,
        retained=set(ordered[11:]),
        ordered=ordered,
        units=units,
    )


def test_submission_expansion_excludes_only_frozen_account_materials(session, partial):
    for unit in partial.units:
        ids = set(
            session.exec(
                select(ExecutionStep.material_id).where(
                    ExecutionStep.submission_id == partial.submission,
                    ExecutionStep.unit_id == unit.unit_id,
                    ExecutionStep.kind == "MATERIAL",
                )
            ).all()
        )
        assert ids == (
            partial.retained if unit.advertiser_id == "A" else set(partial.ordered)
        )


def test_group_dependencies_and_recovery_ignore_skipped_materials(
    session, context, partial
):
    steps = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == partial.submission,
            ExecutionStep.unit_id == partial.unit.id,
            ExecutionStep.kind == "MATERIAL",
        )
    ).all()
    for step in steps:
        if step.material_id in partial.retained:
            step.status = "SUCCEEDED"
            session.add(step)
    session.flush()
    groups = previews.get_frozen_groups(
        session, context=context, unit_id=partial.unit.id
    ).items
    for group in groups:
        assert (
            submissions.group_material_state(
                session,
                context=context,
                submission_id=partial.submission,
                unit_id=partial.unit.id,
                group_id=group.group_id,
            )
            == "READY"
        )
    ready = session.execute(
        text(
            "SELECT "
            + recovery.GROUP_READY
            + " FROM execution_step s WHERE s.submission_id=:submission AND s.unit_id=:unit AND s.kind='CAMPAIGN'"
        ),
        {"submission": partial.submission, "unit": partial.unit.id},
    ).scalar_one()
    assert ready is True


def test_submission_catalog_counts_and_pages_only_retained_materials(
    session, context, partial
):
    units = submissions.get_submission_units(
        session, context=context, submission_id=partial.submission
    ).items
    assert next(u for u in units if u.unit_id == partial.unit.id).material_count == 12
    groups = submission_catalog.get_submission_groups(
        session,
        context=context,
        submission_id=partial.submission,
        unit_id=partial.unit.id,
    ).items
    assert sorted(g.material_count for g in groups) == [3, 9]
    retained = set()
    for group in groups:
        page = submission_catalog.get_submission_materials(
            session,
            context=context,
            submission_id=partial.submission,
            unit_id=partial.unit.id,
            group_id=group.group_id,
            limit=2,
        )
        assert page.total == group.material_count
        while True:
            retained.update(m.material_id for m in page.items)
            if not page.next_cursor:
                break
            page = submission_catalog.get_submission_materials(
                session,
                context=context,
                submission_id=partial.submission,
                unit_id=partial.unit.id,
                group_id=group.group_id,
                limit=2,
                cursor=page.next_cursor,
            )
    assert retained == partial.retained


def test_ad_request_and_final_fence_keep_same_frozen_material_order(
    session, context, partial
):
    group = next(
        g
        for g in previews.get_frozen_groups(
            session, context=context, unit_id=partial.unit.id
        ).items
        if g.group_no == 2
    )
    steps = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.submission_id == partial.submission,
            ExecutionStep.unit_id == partial.unit.id,
        )
    ).all()
    ad = next(s for s in steps if s.kind == "AD" and s.group_id == group.group_id)
    for step in steps:
        if step.kind in {"CTA", "ADGROUP"}:
            step.status, step.remote_id = "SUCCEEDED", f"remote-{step.id}"
            session.add(step)
    # 排除素材随后恢复可用，仍不能重新混入该账户已确认的广告请求。
    for material_id in partial.ordered:
        session.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id=partial.unit.bc_id,
                material_id=material_id,
                advertiser_id="A",
                connection_id=partial.unit.connection_id,
                video_id=f"video-{material_id}",
                image_id=f"image-{material_id}",
                status="available",
                verified_at=datetime.now(UTC),
            )
        )
    session.flush()
    claim = StepClaim.model_validate(
        {
            **ad.model_dump(),
            "step_id": ad.id,
            "actor_id": context.actor_id,
            "advertiser_id": "A",
            "lease_token": uuid4(),
            "attempt_id": uuid4(),
            "lease_expires_at": datetime.now(UTC),
            "route": load_preview_route(
                session, context=context, preview_id=partial.preview
            ),
        }
    )
    body = execution.prepare_request(
        session,
        context=context,
        claim=claim,
        frozen=previews.load_frozen_unit(
            session, context=context, unit_id=partial.unit.id
        ),
    )
    assert [
        c["creative_info"]["video_info"]["video_id"] for c in body["creative_list"]
    ] == [f"video-{identity}" for identity in group.material_ids]
    validate_ad_assets(session, step=ad, unit=partial.unit, body=body)
