"""Malformed fixture cases are synthetic, not recordings of customer assets."""

from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.core.db import engine
from app.core.errors import DomainError
from app.modules.builds import scene
from app.modules.builds.scene_models import SceneEvidence
from tests.modules.builds.scene.test_scene_concurrency import BOUNDED_GUARD
from tests.modules.builds.scene.test_scene_reads import (
    page,
    refresh,
    role_page,
)
from tests.modules.builds.scene.test_scene_reads import (
    scene_env as scene_env,
)
from tests.modules.builds.scene.test_scene_reads import (
    source_env as source_env,
)
from tests.modules.builds.scene.test_scene_reads import (
    wire as wire,
)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("page_info"),
        lambda value: value["page_info"].update(page=True),
        lambda value: value["page_info"].update(page_size=10),
        lambda value: value["page_info"].update(total_page=1001),
        lambda value: value["page_info"].update(total_number=2),
        lambda value: value["list"][0].pop("advertiser_role"),
        lambda value: value["list"][0].update(advertiser_role="ROLE_ADVERTISER"),
        lambda value: value["list"][0].update(asset_type="CATALOG"),
    ],
)
def test_incomplete_role_schema_never_establishes_permission(
    scene_env, wire, redis_client, mutate
):
    from app.modules.accounts.models import BCAccountAccess

    value = role_page()
    mutate(value)
    wire[1].append(value)
    result = refresh(scene_env, redis_client, "account_roles")
    assert not result.complete and result.reason_codes == ("scene_response_unverified",)
    with Session(engine) as session:
        grant = session.exec(select(BCAccountAccess)).one()
        assert grant.permission_state == "UNKNOWN" and not grant.can_build
        assert not session.exec(select(SceneEvidence)).all()


def test_changed_pagination_does_not_publish_partial_identity(
    scene_env, wire, redis_client
):
    wire[1].append(page([{"minis_id": f"other-{n}"} for n in range(50)], total=2))
    first = refresh(scene_env, redis_client, "minis")
    changed = page([{"minis_id": "last"}], number=2, total=2)
    changed["page_info"]["total_number"] = 52
    wire[1].append(changed)
    result = refresh(scene_env, redis_client, "minis", first.evidence_id)
    assert result.reason_codes == ("scene_pagination_changed",)
    with Session(engine) as session:
        assert len(session.exec(select(SceneEvidence)).all()) == 1


@pytest.mark.parametrize(
    "resource,data",
    [
        ("cta", {"recommend_assets": [{"asset_ids": [str(i) for i in range(51)]}]}),
        ("cta", {"recommend_assets": [{"asset_ids": [False]}]}),
        ("vbo", {"vo_min_roas": True}),
        (
            "minis",
            page(
                [
                    {
                        "minis_id": "fixture-minis",
                        "minis_status": "ACTIVE",
                        "minis_type": "MINI_SERIES",
                        "region_codes": ["U1"],
                    }
                ]
            ),
        ),
    ],
)
def test_malformed_asset_facts_are_blocked(
    scene_env, wire, redis_client, resource, data
):
    wire[1].append(data)
    assert refresh(scene_env, redis_client, resource).reason_codes == (
        "scene_response_unverified",
    )


@pytest.mark.parametrize(
    "daemon,name,eager,direct,hard",
    [
        (False, "ForkPoolWorker-1", False, False, 45),
        (True, "MainProcess", False, False, 45),
        (True, "ForkPoolWorker-1", True, False, 45),
        (True, "ForkPoolWorker-1", False, True, 45),
        (True, "ForkPoolWorker-1", False, False, 46),
        (True, "ForkPoolWorker-1", False, False, None),
        (True, "ForkPoolWorker-1", False, False, True),
    ],
)
def test_production_runtime_enforces_prefork_effective_hard_limit(
    monkeypatch, daemon, name, eager, direct, hard
):
    monkeypatch.setattr(
        scene, "current_process", lambda: SimpleNamespace(daemon=daemon, name=name)
    )
    monkeypatch.setattr(
        scene,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(hard, None), is_eager=eager, called_directly=direct
            ),
            time_limit=hard,
        ),
    )
    with pytest.raises(DomainError) as caught:
        BOUNDED_GUARD()
    assert caught.value.code == "scene_worker_unbounded"


def test_valid_production_guard_allows_only_bounded_prefork(monkeypatch):
    monkeypatch.setattr(
        scene,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    monkeypatch.setattr(
        scene,
        "current_task",
        SimpleNamespace(
            request=SimpleNamespace(
                timelimit=(45, None), is_eager=False, called_directly=False
            ),
            time_limit=45,
        ),
    )
    BOUNDED_GUARD()


def test_constraints_have_a_reviewed_version_and_no_unknown_numeric_limit():
    from app.modules.builds import scene_constraints as limits

    constraints, reasons = limits.constraints_for("USD")
    assert constraints["revision"] == limits.REVISION
    assert (
        constraints["copy_length"] == 100
        and constraints["copy_measurement"] == "characters"
    )
    assert constraints["platform_copy_length"] is None
    assert "field_limits_unverified" not in reasons
    other, reasons = limits.constraints_for("UNKNOWN")
    assert "campaign_daily_budget" not in other
    assert "budget_limits_unverified" in reasons
