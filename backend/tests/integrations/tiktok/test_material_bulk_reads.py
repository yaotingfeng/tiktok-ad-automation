"""Bulk lookup transport contracts: a truncated request must fail these tests."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.errors import DomainError
from tests.integrations.tiktok.test_material_adapters import (
    material_case as material_case,
)


def test_23_videos_and_images_use_two_complete_physical_requests(material_case):
    adapter, enqueue, budget, _, calls = material_case
    identities = tuple(str(90000000000000000 + i) for i in range(23))
    for kind in ("video", "image"):
        enqueue(
            f"materials.get_{kind}s",
            {"list": [{f"{kind}_id": value} for value in identities]},
        )
        rows = getattr(adapter, f"read_{kind}s")(
            advertiser_id="123", **{f"{kind}_ids": identities}, budget=budget
        )
        assert tuple(getattr(row, f"{kind}_id") for row in rows) == identities
    if enqueue.channel == "SDK":
        assert len(calls) == 2
        args = [parse_qs(urlsplit(path).query) for path in calls]
        assert json.loads(args[0]["video_ids"][0]) == list(identities)
        assert json.loads(args[1]["image_ids"][0]) == list(identities)
    else:
        requests = [call for call in calls if call.get("method") == "tools/call"]
        assert len(requests) == 2
        assert requests[0]["params"]["arguments"]["video_ids"] == list(identities)
        assert requests[1]["params"]["arguments"]["image_ids"] == list(identities)


@pytest.mark.parametrize("kind", ["video", "image"])
def test_bulk_missing_ids_are_absent_without_relabeling_rows(material_case, kind):
    adapter, enqueue, budget, _, _ = material_case
    enqueue(
        f"materials.get_{kind}s",
        {"list": [{f"{kind}_id": "third"}, {f"{kind}_id": "first"}]},
    )
    rows = getattr(adapter, f"read_{kind}s")(
        advertiser_id="123",
        **{f"{kind}_ids": ("first", "missing", "third")},
        budget=budget,
    )
    assert {getattr(row, f"{kind}_id") for row in rows} == {"first", "third"}


@pytest.mark.parametrize("kind", ["video", "image"])
@pytest.mark.parametrize("bad", ["duplicate", "unrequested", "invalid", "account"])
def test_bulk_ambiguous_response_rejects_whole_result(material_case, kind, bad):
    adapter, enqueue, budget, _, _ = material_case
    row = {f"{kind}_id": "second"}
    if bad == "duplicate":
        row[f"{kind}_id"] = "first"
    elif bad == "unrequested":
        row[f"{kind}_id"] = "other"
    elif bad == "invalid":
        row["width"] = True
    else:
        row["advertiser_id"] = "456"
    enqueue(f"materials.get_{kind}s", {"list": [{f"{kind}_id": "first"}, row]})
    with pytest.raises(DomainError) as error:
        getattr(adapter, f"read_{kind}s")(
            advertiser_id="123", **{f"{kind}_ids": ("first", "second")}, budget=budget
        )
    assert error.value.code == "unsupported_material_schema"


@pytest.mark.parametrize(
    "identities", [(), ("same", "same"), tuple(str(i) for i in range(51))]
)
@pytest.mark.parametrize("kind", ["video", "image"])
def test_bulk_invalid_request_sends_nothing(material_case, identities, kind):
    adapter, _, budget, _, calls = material_case
    before = len(calls)
    with pytest.raises(DomainError):
        getattr(adapter, f"read_{kind}s")(
            advertiser_id="123", **{f"{kind}_ids": identities}, budget=budget
        )
    assert len(calls) == before
