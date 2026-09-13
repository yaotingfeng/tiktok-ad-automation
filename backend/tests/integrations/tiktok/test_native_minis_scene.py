"""原生 Minis 回执形状，均为合成身份/账户，不调用平台。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import IdentityFields
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.read_normalization import scene_arguments, scene_page
from app.modules.builds.request_compiler import decode_intent, encode_intent
from app.modules.builds.scene import _assemble_scene
from tests.contracts.test_tiktok_build_contract import build_bodies


def identity_data():
    return {
        "identity_list": [
            {
                "identity_id": "synthetic-user",
                "identity_type": "TT_USER",
                "identity_authorized_bc_id": None,
                "can_push_video": True,
                "is_gpppa": False,
                "available_status": "AVAILABLE",
            }
        ],
        "page_info": {"page": 0, "page_size": 0, "total_page": 0, "total_number": 0},
    }


def parse(data):
    return scene_page(
        data,
        evidence=CallEvidence(request_id="synthetic"),
        resource="identity",
        page=1,
        advertiser_id="synthetic-advertiser",
        bc_id="synthetic-bc",
        minis_id=None,
    )


def test_account_owned_identity_zero_page_metadata_is_complete_and_not_a_bc_grant():
    result = parse(identity_data())
    assert result.last and result.facts.seen == result.facts.total_number == 1
    match = result.facts.matches[0]
    assert match.identity_type == "TT_USER" and match.identity_authorized_bc_id is None
    _, args = scene_arguments(
        resource="identity",
        advertiser_id="synthetic-advertiser",
        bc_id="synthetic-bc",
        page=1,
        minis_id=None,
    )
    assert "identity_authorized_bc_id" not in args and "identity_type" not in args


@pytest.mark.parametrize(
    "problem", ["foreign_bc", "fake_pagination", "missing_pagination", "duplicate"]
)
def test_identity_does_not_gain_authority_from_invalid_shape(problem):
    data = identity_data()
    if problem == "foreign_bc":
        data["identity_list"][0]["identity_authorized_bc_id"] = "foreign"
    elif problem == "fake_pagination":
        data["page_info"]["total_number"] = 1
    elif problem == "missing_pagination":
        data["page_info"].pop("total_page")
    else:
        data["identity_list"] *= 2
    with pytest.raises(DomainError):
        parse(data)


def test_tt_user_creative_roundtrips_without_inventing_bc_identity():
    body = deepcopy(build_bodies()["AD"])
    for item in body["creative_list"]:
        item["creative_info"]["identity_type"] = "TT_USER"
        item["creative_info"].pop("identity_authorized_bc_id")
    intent = decode_intent("AD", body)
    assert encode_intent(intent) == body
    with pytest.raises(ValidationError):
        IdentityFields(identity_type="BC_AUTH_TT", identity_id="synthetic-user")
    with pytest.raises(ValidationError):
        IdentityFields(
            identity_type="TT_USER",
            identity_id="synthetic-user",
            identity_authorized_bc_id="foreign",
        )


def test_regions_do_not_send_the_rejected_mini_app_enum():
    _, args = scene_arguments(
        resource="regions",
        advertiser_id="synthetic-advertiser",
        bc_id="synthetic-bc",
        page=1,
        minis_id="synthetic-minis",
    )
    assert args["app_promotion_type"] == "MINIS" and "promotion_type" not in args


@pytest.mark.parametrize("qualified", [True, False])
def test_iaa_roas_selects_ad_revenue_only_with_actual_qualification(qualified):
    facts = {
        "vbo": {
            "vo_min_roas": "NOT_SUPPORT",
            "vo_iaa_min_roas_zero_day": "QUALIFIED" if qualified else "NOT_SUPPORT",
        }
    }
    result = _assemble_scene(
        scope={
            "basis": "synthetic",
            "access": SimpleNamespace(currency="USD"),
            "minis_id": "synthetic-minis",
        },
        facts=facts,
        reasons=[],
        evidence_ids=(uuid4(),),
        capability=SimpleNamespace(scope_verified=True, can_build=True),
        locally_operable=True,
    )
    assert ("minis_vbo_unverified" in result.reason_codes) is not qualified
    if qualified:
        assert (
            result.adgroup_fields["optimization_event"] == "IMPRESSION_LEVEL_AD_REVENUE"
        )
