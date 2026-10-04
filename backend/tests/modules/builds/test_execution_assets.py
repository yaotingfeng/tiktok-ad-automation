"""Business asset composition, actual official wire shape, no remote services."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.preview_validation import scene_reasons
from app.modules.builds.request_compiler import (
    ad_assets,
    compile_request,
    cta_portfolio,
    decode_intent,
)
from app.modules.builds.scene_schemas import SceneContext
from app.modules.strategies.copy_pool import seed_copies
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.structure import plan_structure
from tests.integrations.tiktok.build_wire import sdk_build_operations


def _strategy(**changes):
    """构造完全离线的策略快照，测试只消费冻结输入，不访问账户或平台。"""
    return StrategyConfig.model_validate(
        {
            "budget": "100.00",
            "currency": "USD",
            "copy_pool_version": uuid4(),
            "cta_option_ids": ("cta-1",),
            **changes,
        }
    )


def _materials(count: int):
    return [
        SimpleNamespace(
            material_id=uuid4(),
            file_name=f"fixture-{index:02}.mp4",
        )
        for index in range(count)
    ]


def _plan_snapshot(
    config: StrategyConfig,
    count: int = 20,
    materials: list[SimpleNamespace] | None = None,
):
    materials = materials or _materials(count)
    groups = plan_structure(materials, config=config, pool=seed_copies(), seed=17)
    # PreviewAdMaterial 冻结的是基础广告的素材集合，创意复制只增加 PlannedAd。
    ad_materials = {
        (group.group_no, ad.base_ad_no): ad.material_ids
        for group in groups
        for ad in group.ads
    }
    planned_ads = sum(
        len(ad.copies) for group in groups for ad in group.ads
    )
    return groups, planned_ads, ad_materials


def test_end_to_end_by_material_one_material_per_ad_has_frozen_sets_and_fences():
    groups, planned_ads, ad_materials = _plan_snapshot(
        _strategy(
            group_generation_mode="FIXED",
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=1,
        )
    )

    assert len(groups) == 1
    assert planned_ads == 20
    assert len(ad_materials) == 20
    assert all(len(material_ids) == 1 for material_ids in ad_materials.values())
    # CTA and target video/cover fences are checked before any request is compiled.
    body = ad_assets(
        [{"video_id": "target-v1", "image_id": "target-cover"}],
        text="Watch now.",
        url="https://example.test/minis",
        identity={
            "identity_type": "BC_AUTH_TT",
            "identity_id": "identity-1",
            "identity_authorized_bc_id": "bc-1",
        },
    )
    assert body["creative_list"][0]["creative_info"]["image_info"] == [
        {"web_uri": "target-cover"}
    ]
    assert cta_portfolio(
        advertiser_id="account-1",
        assets=[{"asset_ids": ["cta-1"], "asset_content": "Watch now"}],
    )["portfolio_content"][0]["asset_ids"] == ["cta-1"]


def test_end_to_end_two_groups_average_keeps_disjoint_one_material_ad_sets():
    groups, planned_ads, ad_materials = _plan_snapshot(
        _strategy(
            group_count=2,
            group_material_allocation="SEQUENTIAL_AVERAGE",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=1,
        )
    )

    assert tuple(len(group.material_ids) for group in groups) == (10, 10)
    assert planned_ads == 20
    assert tuple(len(group.ads) for group in groups) == (10, 10)
    assert all(len(material_ids) == 1 for material_ids in ad_materials.values())
    assert set(groups[0].material_ids).isdisjoint(groups[1].material_ids)


def test_end_to_end_fixed_shared_creatives_expand_planned_ads_without_duplicate_mapping():
    groups, planned_ads, ad_materials = _plan_snapshot(
        _strategy(
            group_count=1,
            group_material_allocation="SHARED",
            ad_generation_mode="FIXED",
            ads_per_group=2,
            ad_material_allocation="SHARED",
            max_materials_per_ad=None,
            creative_count=3,
        )
    )

    assert planned_ads == 6
    assert tuple(len(group.ads) for group in groups) == (2,)
    assert set(ad_materials) == {(1, 1), (1, 2)}
    assert all(len(material_ids) == 20 for material_ids in ad_materials.values())
    assert groups[0].ads[0].material_ids == groups[0].ads[1].material_ids
    assert all(len(ad.copies) == 3 for ad in groups[0].ads)


def test_end_to_end_group_budget_highest_value_payload_has_correct_layer_and_bid():
    groups, planned_ads, _ = _plan_snapshot(
        _strategy(
            budget_strategy="ADGROUP",
            bid_strategy="HIGHEST_VALUE",
            group_count=2,
            group_material_allocation="SHARED",
            ad_generation_mode="BY_MATERIAL",
            ads_per_group=None,
            ad_material_allocation=None,
            max_materials_per_ad=2,
        )
    )
    assert tuple(len(group.ads) for group in groups) == (10, 10)
    assert planned_ads == 20
    campaign = compile_request(
        "campaign",
        fixed={
            "advertiser_id": "account-1",
            "campaign_name": "fixture",
            "budget_strategy": "ADGROUP",
        },
        resolved={},
    )
    adgroup = compile_request(
        "adgroup",
        fixed={
            "advertiser_id": "account-1",
            "campaign_id": "campaign-1",
            "adgroup_name": "fixture-group",
            "budget": 100,
            "budget_strategy": "ADGROUP",
            "bid_strategy": "HIGHEST_VALUE",
        },
        resolved={"targeting_spec": {"location_ids": ["US"]}},
    )
    assert campaign["budget_mode"] == "BUDGET_MODE_INFINITE"
    assert "budget" not in campaign
    assert adgroup["budget"] == 100
    assert adgroup["deep_bid_type"] == "VO_HIGHEST_VALUE"
    assert "roas_bid" not in adgroup


def test_two_drafts_reuse_strategy_without_sharing_preview_frozen_rows():
    strategy_version_id = uuid4()
    config = _strategy(group_count=1, group_material_allocation="SHARED")
    strategy_version = {
        "id": strategy_version_id,
        "number": 3,
        "config": config.model_dump(mode="json"),
    }
    strategy_before = deepcopy(strategy_version)
    first_preview, second_preview = uuid4(), uuid4()
    groups, _, ad_materials = _plan_snapshot(config, count=4)
    first_rows = {
        (first_preview, group_no, base_ad_no, position, material_id)
        for (group_no, base_ad_no), material_ids in ad_materials.items()
        for position, material_id in enumerate(material_ids, 1)
    }
    second_rows = {
        (second_preview, group_no, base_ad_no, position, material_id)
        for (group_no, base_ad_no), material_ids in ad_materials.items()
        for position, material_id in enumerate(material_ids, 1)
    }
    assert first_rows.isdisjoint(second_rows)
    assert strategy_version == strategy_before
    assert config.model_dump()["creative_count"] == 1
    assert len(groups) == 1


def test_shared_materials_prepare_one_account_task_but_keep_per_ad_creative_lists():
    shared_mapping = {"video_id": "video-shared", "image_id": "cover-shared"}
    ad_one = ad_assets(
        [shared_mapping],
        text="A",
        url="https://example.test/a",
        identity={
            "identity_type": "BC_AUTH_TT",
            "identity_id": "identity-1",
            "identity_authorized_bc_id": "bc-1",
        },
    )
    ad_two = ad_assets(
        [shared_mapping],
        text="B",
        url="https://example.test/b",
        identity={
            "identity_type": "BC_AUTH_TT",
            "identity_id": "identity-1",
            "identity_authorized_bc_id": "bc-1",
        },
    )
    account_material_keys = {("account-1", "material-shared")}
    assert len(account_material_keys) == 1
    assert ad_one["creative_list"] == ad_two["creative_list"]
    assert ad_one["ad_text_list"] != ad_two["ad_text_list"]


def test_failure_and_recovery_fences_do_not_replan_or_expand_materials():
    config = _strategy(
        group_count=2,
        group_material_allocation="SEQUENTIAL_AVERAGE",
        ad_generation_mode="BY_MATERIAL",
        ads_per_group=None,
        ad_material_allocation=None,
        max_materials_per_ad=1,
    )
    frozen_materials = _materials(20)
    before, count_before, mapping_before = _plan_snapshot(
        config, materials=frozen_materials
    )
    with pytest.raises(DomainError, match="目标账户素材尚未核实"):
        ad_assets(
            [{"video_id": "", "image_id": "pending-cover"}],
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity-1",
                "identity_authorized_bc_id": "bc-1",
            },
        )
    with pytest.raises(DomainError, match="目标账户素材尚未核实"):
        ad_assets(
            [{"video_id": "verified-video", "image_id": ""}],
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity-1",
                "identity_authorized_bc_id": "bc-1",
            },
        )
    recovered, count_after, mapping_after = _plan_snapshot(
        config, materials=frozen_materials
    )
    assert count_after == count_before
    assert mapping_after == mapping_before
    assert [group.material_ids for group in recovered] == [
        group.material_ids for group in before
    ]

    scene = SceneContext(
        supported=True,
        reason_codes=(),
        capability_revision="fixture",
        creative_limit=50,
        copy_length_limit=100,
        field_constraints={
            "max_ads_per_adgroup": 30,
            "campaign_daily_budget": {
                "currency": "USD",
                "minimum_inclusive": "1",
                "maximum_exclusive": "1000",
                "precision": "1",
            },
            "bid_capabilities": {"HIGHEST_VALUE": {}},
        },
        cta_fields={"asset_ids": ["cta-1"]},
    )
    reasons = scene_reasons(
        _strategy(cta_option_ids=("cta-1",)), scene, "USD"
    )
    assert "bid_strategy_invalid" in reasons
    assert "adgroup_budget_unverified" not in reasons


def test_each_sp_uses_complete_target_group_and_one_independent_text():
    from app.modules.builds.request_compiler import ad_assets

    mappings = [
        {
            "video_id": "target-v1",
            "image_id": "target-cover-1",
            "file_name": "01-The General-CL6-LH-1.mp4",
        },
        {"video_id": "target-v2", "image_id": "target-cover-2"},
    ]
    identity = {
        "identity_type": "BC_AUTH_TT",
        "identity_id": "identity-1",
        "identity_authorized_bc_id": "bc-1",
    }
    before = deepcopy((mappings, identity))
    a = ad_assets(
        mappings,
        text="Watch an episode.",
        url="https://example.test/minis",
        identity=identity,
    )
    b = ad_assets(
        mappings,
        text="Follow the story.",
        url="https://example.test/minis",
        identity=identity,
    )
    assert a["creative_list"] == b["creative_list"]
    assert [
        c["creative_info"]["video_info"]["video_id"] for c in a["creative_list"]
    ] == ["target-v1", "target-v2"]
    assert [c["creative_info"]["image_info"] for c in a["creative_list"]] == [
        [{"web_uri": "target-cover-1"}],
        [{"web_uri": "target-cover-2"}],
    ]
    assert (
        a["creative_list"][0]["creative_info"]["video_info"]["file_name"]
        == "01-The General-CL6-LH-1.mp4"
    )
    from app.modules.builds.request_compiler import encode_intent

    request = {
        **a,
        "advertiser_id": "account",
        "adgroup_id": "group",
        "ad_name": "ad",
        "operation_status": "ENABLE",
        "ad_configuration": {"call_to_action_id": "cta"},
    }
    assert encode_intent(decode_intent("AD", request)) == request
    assert a["ad_text_list"] == [{"ad_text": "Watch an episode."}]
    assert b["ad_text_list"] == [{"ad_text": "Follow the story."}]
    assert (mappings, identity) == before


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        [{"video_id": "target", "image_id": True}],
        [{"video_id": " ", "image_id": "cover"}],
    ],
)
def test_unverified_target_mapping_never_compiles(mappings):
    from app.modules.builds.request_compiler import ad_assets

    with pytest.raises(DomainError):
        ad_assets(
            mappings,
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity",
                "identity_authorized_bc_id": "bc",
            },
        )


def test_identity_cannot_override_creative_assets():
    from app.modules.builds.request_compiler import ad_assets

    with pytest.raises(DomainError):
        ad_assets(
            [{"video_id": "target", "image_id": "cover"}],
            text="Watch.",
            url="https://example.test",
            identity={
                "identity_type": "BC_AUTH_TT",
                "identity_id": "identity",
                "identity_authorized_bc_id": "bc",
                "video_info": {"video_id": "source"},
            },
        )


def test_official_cta_portfolio_wire_uses_actual_recommendation_ids(monkeypatch):

    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, json.loads(kwargs["body"])))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {"creative_portfolio_id": "portfolio-1"},
                    "request_id": "receipt-1",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    body = cta_portfolio(
        advertiser_id="advertiser-1",
        assets=(
            {
                "asset_ids": ["actual-cta-1", "actual-cta-2"],
                "asset_content": "Watch now",
            },
        ),
    )
    with official_client(access_token="fixture-token") as client:
        result = sdk_build_operations(client).create(
            attempt_id=uuid4(), intent=decode_intent("CTA", body)
        )
    assert result.remote_id == "portfolio-1"
    assert calls == [
        (
            "POST",
            "https://business-api.tiktok.com/open_api/v1.3/creative/portfolio/create/",
            {
                "advertiser_id": "advertiser-1",
                "creative_portfolio_type": "CTA",
                "portfolio_content": [
                    {
                        "asset_ids": ["actual-cta-1", "actual-cta-2"],
                        "asset_content": "Watch now",
                    }
                ],
            },
        )
    ]


def test_portfolio_unknown_response_is_not_retryable_proof(monkeypatch):

    calls = []

    def request(_pool, method, _url, **_kwargs):
        calls.append(method)
        return HTTPResponse(
            body=b'{"code":0,"data":{},"request_id":"receipt-1"}', status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(RemoteCallError) as caught:
            sdk_build_operations(client).create(
                attempt_id=uuid4(),
                intent=decode_intent(
                    "CTA",
                    cta_portfolio(
                        advertiser_id="advertiser-1",
                        assets=(
                            {
                                "asset_ids": ["actual-cta-1"],
                                "asset_content": "Watch now",
                            },
                        ),
                    ),
                ),
            )
    assert (
        caught.value.effect == "UNKNOWN"
        and caught.value.evidence.request_id == "receipt-1"
        and calls == ["POST"]
    )
