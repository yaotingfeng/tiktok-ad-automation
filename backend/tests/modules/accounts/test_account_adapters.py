import json

import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok import accounts
from app.integrations.tiktok.sdk import official_client


@pytest.mark.parametrize(
    "method,kwargs,path,expected",
    [
        (
            accounts.read_authorized_advertisers,
            {"app_id": "app", "secret": "secret"},
            "/oauth2/advertiser/get/",
            {"app_id": "app", "secret": "secret"},
        ),
        (
            accounts.read_business_centers,
            {"page": 2, "page_size": 100},
            "/bc/get/",
            {"page": 2, "page_size": 100},
        ),
        (
            accounts.read_bc_assets,
            {"bc_id": "90071992547409931", "page": 2, "page_size": 100},
            "/bc/asset/get/",
            {
                "bc_id": "90071992547409931",
                "asset_type": "ADVERTISER",
                "page": 2,
                "page_size": 100,
            },
        ),
        (
            accounts.read_advertiser_details,
            {"advertiser_ids": ["90071992547409931"]},
            "/advertiser/info/",
            {"advertiser_ids": ["90071992547409931"]},
        ),
    ],
)
def test_generated_sdk_transport_contract(monkeypatch, method, kwargs, path, expected):
    calls = []

    def request(_pool, verb, url, **options):
        calls.append((verb, url, options))
        return HTTPResponse(
            body=json.dumps(
                {"code": 0, "data": {"list": []}, "request_id": "r"}
            ).encode(),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="candidate-fake") as client:
        assert method(client, **kwargs) == {"list": []}
        assert client.rest_client.pool_manager.connection_pool_kw["retries"].total == 0
    assert len(calls) == 1
    verb, url, options = calls[0]
    assert verb == "GET" and url.endswith(path)
    assert options["headers"]["Access-Token"] == "candidate-fake"
    fields = dict(options["fields"])
    for key, value in expected.items():
        actual = fields[key]
        if isinstance(value, list):
            actual = json.loads(actual)
        assert actual == value
    if method is accounts.read_authorized_advertisers:
        assert "page" not in fields


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"list": []},
        {"list": [], "page_info": {}},
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 2}},
        {"list": [], "page_info": {"page": True, "page_size": 100, "total_page": 1}},
        {"list": {}, "page_info": {"page": 1, "page_size": 100, "total_page": 1}},
    ],
)
def test_paging_fails_closed_without_valid_end_evidence(data):
    with pytest.raises(DomainError) as error:
        accounts.paged_rows(data, page=1, page_size=100)
    assert error.value.code == "unsupported_account_schema"


def test_empty_and_nonempty_end_evidence():
    assert accounts.paged_rows(
        {"list": [], "page_info": {"page": 1, "page_size": 100, "total_page": 0}},
        page=1,
        page_size=100,
    ) == ([], True)
    assert accounts.paged_rows(
        {"list": [{}], "page_info": {"page": 1, "page_size": 100, "total_page": 2}},
        page=1,
        page_size=100,
    ) == ([{}], False)


def test_unknown_capabilities_are_not_normalized_from_unverified_fields():
    normalized = accounts.detail_row(
        {
            "advertiser_id": "90071992547409931",
            "role": "ADMIN",
            "can_upload": True,
            "can_build": True,
        }
    )
    assert normalized == {
        "advertiser_id": "90071992547409931",
        "name": "",
        "currency": "",
        "timezone": "",
        "remote_status": "UNKNOWN",
    }
    with pytest.raises(DomainError):
        accounts.external_id({"advertiser_id": 90071992547409931}, "advertiser_id")
