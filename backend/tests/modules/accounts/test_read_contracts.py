from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.integrations.tiktok.contracts.accounts import (
    AccountRoleFact,
    AdvertiserFact,
    AuthorizationFacts,
    BusinessCenterFact,
    DirectoryPage,
)
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.scenes import RegionFacts, ScenePage, VboFacts


def test_large_advertiser_id_stays_exact_and_unknown_stays_unknown():
    row = AdvertiserFact("9007199254740993123", "Test", "USD", "UTC", "ENABLE", None)
    assert row.advertiser_id == "9007199254740993123"
    assert row.authorized is None
    with pytest.raises((FrozenInstanceError, ValidationError)):
        row.name = "changed"
    with pytest.raises((ValueError, TypeError)):
        AdvertiserFact(9007199254740993123.0, "Test", "USD", "UTC", "ENABLE", None)


def test_authorization_unknown_is_not_role_or_scope_inference():
    facts = AuthorizationFacts(
        None,
        None,
        "https://issuer.example",
        "https://resource.example",
        ("write",),
        None,
        None,
        None,
        "synthetic",
        datetime.now(UTC),
    )
    assert (
        facts.read_authorized
        is facts.upload_authorized
        is facts.build_authorized
        is None
    )
    assert AccountRoleFact("123", None).role is None
    with pytest.raises((ValueError, TypeError)):
        AccountRoleFact("123", "OWNER")


@pytest.mark.parametrize(
    "changes",
    [
        {"page": 0},
        {"page": True},
        {"page": 2},
        {"page_size": 0},
        {"total_pages": -1},
        {"total_number": -1},
        {"total_number": True},
        {"last": False},
        {"items": (BusinessCenterFact("1", "a"), BusinessCenterFact("1", "b"))},
        {"total_number": 0},
        {"total_pages": 0},
    ],
)
def test_directory_page_rejects_invalid_metadata_and_duplicate_ids(changes):
    args = {
        "items": (BusinessCenterFact("1", "a"),),
        "page": 1,
        "page_size": 50,
        "total_pages": 1,
        "total_number": 1,
        "last": True,
        "evidence": CallEvidence("r", "m", "t"),
    }
    args.update(changes)
    with pytest.raises((ValueError, TypeError)):
        DirectoryPage(**args)


def test_empty_directory_and_all_evidence_are_preserved():
    page = DirectoryPage(
        items=(),
        page=1,
        page_size=50,
        total_pages=0,
        total_number=0,
        last=True,
        evidence=CallEvidence("r", "m", "t"),
    )
    assert page.request_id == "r"
    assert page.evidence.mcp_request_id == "m"
    assert page.evidence.remote_task_id == "t"


def test_scene_facts_are_typed_frozen_and_preserve_exact_decimal():
    facts = VboFacts(vo_min_roas="90071992547409931.123456789")
    page = ScenePage(
        resource="vbo", page=1, last=True, facts=facts, evidence=CallEvidence("r")
    )
    assert page.facts.model_dump(exclude_none=True) == {
        "vo_min_roas": "90071992547409931.123456789"
    }
    assert page.request_id == "r"
    with pytest.raises(ValidationError):
        facts.vo_min_roas = "0"
    for invalid in ({}, {"vo_min_roas": 1.23}, {"vo_status": "ON", "unknown": True}):
        with pytest.raises(ValidationError):
            VboFacts(**invalid)
    with pytest.raises(ValidationError):
        ScenePage(
            resource="vbo",
            page=1,
            last=True,
            facts=RegionFacts(locations=()),
            evidence=CallEvidence(),
        )
    with pytest.raises(ValidationError):
        ScenePage(
            resource="vbo", page=0, last=True, facts=facts, evidence=CallEvidence()
        )


def test_directory_totals_cannot_contradict_returned_page():
    for total, pages in [(2, 1), (1, 2), (51, 1)]:
        with pytest.raises(ValueError):
            DirectoryPage(
                items=(BusinessCenterFact("1", "a"),),
                page=1,
                page_size=50,
                total_pages=pages,
                total_number=total,
                last=pages == 1,
                evidence=CallEvidence(),
            )


def test_scene_page_rejects_untyped_remote_json():
    with pytest.raises(ValidationError):
        ScenePage(
            resource="vbo",
            page=1,
            last=True,
            facts={"vo_status": "ON"},
            evidence=CallEvidence(),
        )


def test_scene_dto_rejects_duplicate_locations_and_matched_ids():
    from app.integrations.tiktok.contracts.scenes import IdentityFacts, RegionLocation

    with pytest.raises(ValidationError):
        RegionFacts(
            locations=(
                RegionLocation(region_code="US", location_id="1"),
                RegionLocation(region_code="US", location_id="2"),
            )
        )
    match = {
        "identity_id": "id",
        "identity_type": "BC_AUTH_TT",
        "identity_authorized_bc_id": "bc",
    }
    with pytest.raises(ValidationError):
        IdentityFacts(
            matches=[match, match],
            item_id_hashes=("a", "b"),
            total_number=2,
            total_page=1,
            seen=2,
        )


@pytest.mark.parametrize(
    "total, pages, seen, page, last",
    [
        (2, 1, 1, 1, True),
        (1, 2, 1, 1, False),
        (51, 2, 1, 1, False),
    ],
)
def test_scene_pagination_rejects_incomplete_counts(total, pages, seen, page, last):
    from app.integrations.tiktok.contracts.scenes import RoleFacts

    with pytest.raises(ValidationError):
        ScenePage(
            resource="account_roles",
            page=page,
            last=last,
            facts=RoleFacts(
                matches=(),
                item_id_hashes=("a",),
                total_number=total,
                total_page=pages,
                seen=seen,
            ),
            evidence=CallEvidence(),
        )
