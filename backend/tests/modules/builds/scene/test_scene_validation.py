"""场景字段与真正 worker 宿主约束；合成数据不代表线上能力。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import DomainError
from app.modules.builds import scene

BOUNDED_GUARD = scene._require_bounded_worker


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "problem",
    [
        "foreign_identity",
        "duplicate_identity",
        "missing_total",
        "wrong_role",
        "minis_regions",
        "cta",
        "vbo",
        "region",
    ],
)
def test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary(
    database_engine, redis_client, scene_case, gateway_wire, problem
):
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from tests.integrations.tiktok.gateway_support import business_calls
    from tests.modules.builds.scene.support import enqueue, page, scene_responses

    case = scene_case
    resource = "identity"
    data = scene_responses(case)[resource]
    if problem == "foreign_identity":
        data["identity_list"][0]["identity_authorized_bc_id"] = "other-bc"
    elif problem == "duplicate_identity":
        data = page(data["identity_list"] * 2, key="identity_list")
    elif problem == "missing_total":
        data["page_info"].pop("total_number")
    elif problem == "wrong_role":
        resource = "account_roles"
        data = page(
            [
                {
                    "asset_id": case["advertiser_id"],
                    "asset_type": "ADVERTISER",
                    "advertiser_role": "OWNER",
                }
            ]
        )
    elif problem == "minis_regions":
        resource = "minis"
        data = scene_responses(case)[resource]
        data["list"][0]["region_codes"] = ["U1"]
    elif problem == "cta":
        resource, data = "cta", {"recommend_assets": [{"asset_ids": [False]}]}
    elif problem == "vbo":
        resource, data = "vbo", {"vo_min_roas": True}
    else:
        resource = "regions"
        data = scene_responses(case)[resource]
        data["region_info"][0].pop("location_id")
    enqueue(gateway_wire, resource, data)
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=case["context"],
        route=case["route"],
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
    ) as gateway:
        with pytest.raises(DomainError) as error:
            gateway.scenes.read_page(
                resource=resource,
                advertiser_id=case["advertiser_id"],
                page=1,
                minis_id="synthetic-minis",
            )
        assert error.value.code == "scene_response_unverified"
    assert len(business_calls(gateway_wire, case["route"].channel)) == 1


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
def test_scene_page_limit_rejects_before_physical_business_call(
    database_engine, redis_client, scene_case, gateway_wire
):
    from app.integrations.tiktok.gateway import open_tiktok_gateway
    from tests.integrations.tiktok.gateway_support import business_calls
    from tests.modules.builds.scene.support import enqueue, scene_responses

    case = scene_case
    enqueue(gateway_wire, "identity", scene_responses(case)["identity"])
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=case["context"],
        route=case["route"],
        task_deadline=datetime.now(UTC) + timedelta(seconds=20),
    ) as gateway:
        with pytest.raises(DomainError) as error:
            gateway.scenes.read_page(
                resource="identity",
                advertiser_id=case["advertiser_id"],
                page=1001,
                minis_id=None,
            )
        assert business_calls(gateway_wire, case["route"].channel) == []
        assert error.value.code == "scene_request_invalid"


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
