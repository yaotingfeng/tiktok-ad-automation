"""单条素材失败不能丢弃同剧的可用素材，冻结后也不能悄悄重新加入。"""

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.modules.builds import previews
from app.modules.builds.models import DraftGroupMaterial
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


@pytest.mark.parametrize("failed_count", [1, 10, 23])
def test_partial_materials_keep_valid_groups_and_frozen_selection(
    session, context, prepared, monkeypatch, failed_count
):
    selected = session.exec(
        select(DraftGroupMaterial)
        .where(DraftGroupMaterial.draft_id == prepared)
        .order_by(
            DraftGroupMaterial.drama_id,
            DraftGroupMaterial.group_no,
            DraftGroupMaterial.position,
        )
    ).all()
    drama_id = selected[0].drama_id
    ordered = [r.material_id for r in selected if r.drama_id == drama_id]
    failed = set(ordered[:failed_count])
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(
                state="blocked" if identity in failed else "preparable",
                reason_code="material_remote_source_unavailable"
                if identity in failed
                else None,
            )
            for identity in kw["material_ids"]
        },
    )
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    units = previews.get_preview_units(
        session, context=context, preview_id=identity, drama_id=drama_id
    ).items
    assert len(units) == 3
    for unit in units:
        assert unit.readiness == ("BLOCKED" if failed_count == 23 else "PREPARING")
        groups = previews.get_frozen_groups(
            session, context=context, unit_id=unit.unit_id
        )
        retained = {m for g in groups.items for m in g.material_ids}
        assert retained == set(ordered) - failed
        assert all(g.material_ids for g in groups.items)
        assert unit.group_count == (
            0 if failed_count == 23 else 2 if failed_count == 10 else 3
        )
        skipped = previews.get_skipped_materials(
            session, context=context, unit_id=unit.unit_id
        )
        assert skipped.total == failed_count
        assert {m.material_id for m in skipped.items} == failed
    summary = previews.get_preview_summary(
        session, context=context, preview_id=identity
    )
    assert summary.campaign_count == (3 if failed_count == 23 else 6)
    assert summary.daily_budget_sum == (300 if failed_count == 23 else 600)
    assert summary.skipped_material_count == failed_count


def test_account_permission_failure_is_not_downgraded_to_material_warning(
    session, context, prepared, monkeypatch
):
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(
                state="blocked", reason_code="account_access_denied"
            )
            for identity in kw["material_ids"]
        },
    )
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    units = previews.get_preview_units(
        session, context=context, preview_id=identity
    ).items
    assert all(
        u.readiness == "BLOCKED" and "account_access_denied" in u.reason_codes
        for u in units
    )


def test_group_limit_applies_to_retained_materials_not_skipped_ones(
    session, context, prepared, monkeypatch
):
    from dataclasses import replace

    read_scene = previews.read_scene_context
    monkeypatch.setattr(
        previews,
        "read_scene_context",
        lambda *a, **kw: replace(read_scene(*a, **kw), creative_limit=9),
    )
    failed = set(
        session.exec(
            select(DraftGroupMaterial.material_id).where(
                DraftGroupMaterial.draft_id == prepared,
                DraftGroupMaterial.position == 1,
            )
        ).all()
    )
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(
                state="blocked" if identity in failed else "ready",
                reason_code="material_remote_source_unavailable"
                if identity in failed
                else None,
            )
            for identity in kw["material_ids"]
        },
    )
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    units = previews.get_preview_units(
        session, context=context, preview_id=preview
    ).items
    assert all(u.readiness == "READY" for u in units)
    for unit in units:
        groups = previews.get_frozen_groups(
            session, context=context, unit_id=unit.unit_id
        ).items
        assert [len(g.material_ids) for g in groups] == [9, 9, 2]


def test_exclusions_are_account_scoped_paginated_authorized_and_immutable(
    session, client, context, other_context, prepared, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from tests.modules.strategies.test_api import headers

    selected = session.exec(
        select(DraftGroupMaterial)
        .where(DraftGroupMaterial.draft_id == prepared)
        .order_by(DraftGroupMaterial.drama_id, DraftGroupMaterial.position)
    ).all()
    drama_id = selected[0].drama_id
    failed = {r.material_id for r in selected if r.drama_id == drama_id}
    monkeypatch.setattr(
        previews,
        "get_material_readiness_batch",
        lambda *a, **kw: {
            identity: SimpleNamespace(
                state="blocked"
                if identity in failed and kw["advertiser_id"] == "A"
                else "ready",
                reason_code="material_remote_source_unavailable"
                if identity in failed
                else None,
            )
            for identity in kw["material_ids"]
        },
    )
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    units = previews.get_preview_units(
        session, context=context, preview_id=preview, drama_id=drama_id
    ).items
    blocked = next(u for u in units if u.advertiser_id == "A")
    assert blocked.readiness == "BLOCKED" and blocked.skipped_material_count == 23
    for unit in units:
        if unit.advertiser_id != "A":
            assert unit.readiness == "READY" and unit.skipped_material_count == 0
            assert (
                sum(
                    len(g.material_ids)
                    for g in previews.get_frozen_groups(
                        session, context=context, unit_id=unit.unit_id
                    ).items
                )
                == 23
            )
    path = f"/api/tenants/{context.tenant_id}/build-units/{blocked.unit_id}/skipped-materials"
    response = client.get(path, params={"limit": 10}, headers=headers(context))
    assert response.status_code == 200
    first = response.json()
    assert first["total"] == 23 and len(first["items"]) == 10 and first["next_cursor"]
    second = client.get(
        path,
        params={"limit": 10, "cursor": first["next_cursor"]},
        headers=headers(context),
    ).json()
    assert not {m["material_id"] for m in first["items"]} & {
        m["material_id"] for m in second["items"]
    }
    assert client.get(path, headers=headers(other_context)).status_code == 403
    # 冻结后即使直接写数据库，也不能删掉证据让素材重新进入广告。
    for statement in [
        "DELETE FROM preview_skipped_material WHERE unit_id=:unit",
        "UPDATE preview_skipped_material SET reason_code='changed' WHERE unit_id=:unit",
        "INSERT INTO preview_skipped_material SELECT * FROM preview_skipped_material WHERE unit_id=:unit LIMIT 1",
    ]:
        with pytest.raises(DBAPIError, match="frozen preview is immutable"):
            with session.begin_nested():
                session.execute(text(statement), {"unit": blocked.unit_id})
