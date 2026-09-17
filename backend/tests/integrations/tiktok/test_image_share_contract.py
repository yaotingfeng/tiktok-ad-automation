"""Official image info/search MID and partial share receipts, both transports.

TikTok docs 1740051721711618, 1740052016789506 and 1773192725768193.
"""

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.contracts.materials import AssetShare
from tests.integrations.tiktok.test_material_adapters import (
    material_case as material_case,
)


def test_image_detail_preserves_mid_for_sharing(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        "materials.get_images",
        {"list": [{"image_id": "tos-source", "material_id": "1234567890"}]},
    )
    image = adapter.read_image(
        advertiser_id="123", image_id="tos-source", budget=budget
    )
    assert getattr(image, "mid", None) == "1234567890"


def test_image_search_preserves_real_target_id_and_mid(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        "materials.search_images",
        {
            "list": [{"image_id": "tos-target", "material_id": "9876543210"}],
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_number": 1,
                "total_page": 1,
            },
        },
    )
    page = adapter.search_images(
        advertiser_id="456", page=1, material_ids=("9876543210",), budget=budget
    )
    assert (page.rows[0].image_id, page.rows[0].mid) == ("tos-target", "9876543210")


def test_image_share_preserves_partial_failure(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue("materials.share_assets", {"failed_infos": {"456": ["1234567890"]}})
    receipt = adapter.share_assets(
        AssetShare("123", ("1234567890",), ("456",), "IMAGE"), budget=budget
    )
    assert getattr(receipt, "failed_infos", None) == {"456": ("1234567890",)}
    assert receipt.evidence.request_id == "material-request"


@pytest.mark.parametrize("returned_size", [71, 100])
def test_image_census_page_size_is_explicit_and_response_must_match(
    material_case, returned_size
):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        "materials.search_images",
        {
            "list": [{"image_id": "target-image", "material_id": "12345"}],
            "page_info": {
                "page": 1,
                "page_size": returned_size,
                "total_number": 1,
                "total_page": 1,
            },
        },
    )
    if returned_size != 71:
        with pytest.raises(DomainError):
            adapter.search_images(
                advertiser_id="456", page=1, page_size=71, budget=budget
            )
    else:
        result = adapter.search_images(
            advertiser_id="456", page=1, page_size=71, budget=budget
        )
        assert result.page_size == 71 and result.rows[0].image_id == "target-image"


def test_unknown_partial_failure_identity_is_not_acknowledged(material_case):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        "materials.share_assets", {"failed_infos": {"foreign-account": ["1234567890"]}}
    )
    with pytest.raises(RemoteCallError) as error:
        adapter.share_assets(
            AssetShare("123", ("1234567890",), ("456",), "IMAGE"), budget=budget
        )
    assert error.value.effect == "UNKNOWN"


def test_unhashable_image_filter_is_a_not_sent_contract_error(material_case):
    adapter, _, budget, _, _ = material_case
    with pytest.raises(DomainError) as error:
        adapter.search_images(
            advertiser_id="456", page=1, material_ids=(["bad"],), budget=budget
        )
    assert error.value.code == "material_request_invalid"
