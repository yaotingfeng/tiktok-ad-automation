"""Pinned official SDK serialization, synthetic IDs, no external service calls."""

import json
from copy import deepcopy

import business_api_client as sdk
import pytest
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import official_client


def fixed(kind):
    return {
        "campaign": {
            "advertiser_id": "fixture-account",
            "campaign_name": "Campaign",
            "budget": 100,
        },
        "adgroup": {
            "advertiser_id": "fixture-account",
            "campaign_id": "campaign-1",
            "adgroup_name": "Group",
            "roas_bid": 1.5,
        },
        "ad": {
            "advertiser_id": "fixture-account",
            "adgroup_id": "group-1",
            "ad_name": "Ad",
        },
    }[kind]


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
def test_compiler_preserves_fixed_intent_and_enables_every_layer(kind):
    from app.modules.builds.sdk_requests import compile_request

    intent, resolved = (
        fixed(kind),
        {"minis_id": "fixture-minis"} if kind == "adgroup" else {},
    )
    before = deepcopy((intent, resolved))
    body = compile_request(kind, fixed=intent, resolved=resolved)
    assert body["operation_status"] == "ENABLE"
    assert all(body[k] == v for k, v in intent.items())
    assert (intent, resolved) == before
    if kind == "campaign":
        assert body["budget_optimize_on"] is True
    if kind == "adgroup":
        assert body["minis_id"] == "fixture-minis" and "app_id" not in body


@pytest.mark.parametrize(
    "key",
    [
        "advertiser_id",
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
        "budget",
        "budget_optimize_on",
        "roas_bid",
        "operation_status",
    ],
)
def test_scene_cannot_override_any_frozen_field(key):
    from app.modules.builds.sdk_requests import compile_request

    with pytest.raises(DomainError) as caught:
        compile_request(
            "campaign", fixed=fixed("campaign"), resolved={key: "untrusted"}
        )
    assert caught.value.code == "scene_overrides_frozen_fields"


def test_group_never_gets_budget_and_unknown_kind_is_rejected():
    from app.modules.builds.sdk_requests import compile_request

    with pytest.raises(DomainError) as caught:
        compile_request(
            "adgroup", fixed={**fixed("adgroup"), "budget": 50}, resolved={}
        )
    assert caught.value.code == "adgroup_budget_not_allowed"
    with pytest.raises(DomainError) as caught:
        compile_request("activate", fixed={}, resolved={})
    assert caught.value.code == "invalid_build_kind"


def test_official_method_accepts_unmodeled_dictionary(monkeypatch):
    # documented_extra proves native-dict serialization only; never production data.
    with official_client(access_token="fixture-token") as client:
        captured = {}

        def capture(path, method, *_args, **kwargs):
            captured.update(
                path=path,
                method=method,
                body=client.sanitize_for_serialization(kwargs["body"]),
            )
            return sdk.InlineResponse200(code=0, data={"adgroup_id": "group-1"})

        monkeypatch.setattr(client, "call_api", capture)
        sdk.AdgroupApi(client).smart_plus_adgroup_create(
            "fixture-token",
            body={
                "advertiser_id": "fixture-account",
                "operation_status": "ENABLE",
                "documented_extra": "kept",
                "minis_id": "fixture-minis",
            },
        )
        assert captured["path"] == "/open_api/v1.3/smart_plus/adgroup/create/"
        assert captured["body"]["documented_extra"] == "kept"
        assert captured["body"]["minis_id"] == "fixture-minis"


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("campaign", "campaign_id"),
        ("adgroup", "adgroup_id"),
        ("ad", "smart_plus_ad_id"),
    ],
)
def test_actual_official_transport_serializes_each_layer(monkeypatch, kind, id_key):
    from app.modules.builds.sdk_requests import compile_request, invoke_create

    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return HTTPResponse(
            body=json.dumps(
                {
                    "code": 0,
                    "data": {id_key: "remote-1", "operation_status": "ENABLE"},
                    "request_id": "request-1",
                }
            ).encode(),
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        body = compile_request(
            kind,
            fixed=fixed(kind),
            resolved={"minis_id": "fixture-minis"} if kind == "adgroup" else {},
        )
        result = invoke_create(client, kind=kind, body=body)
    assert result.remote_id == "remote-1" and result.request_id == "request-1"
    assert result.operation_status == "ENABLE"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith(f"/smart_plus/{kind}/create/")
    assert json.loads(kwargs["body"]) == body
    assert kwargs["headers"]["Access-Token"] == "fixture-token"


@pytest.mark.parametrize(
    "response,code",
    [
        (
            {
                "code": 40002,
                "message": "fixture-token-secret",
                "request_id": "request-2",
                "data": {},
            },
            40002,
        ),
        ({"code": 0, "data": {}, "request_id": "request-2"}, 0),
        (
            {
                "code": 0,
                "data": {"ad_id": "legacy-not-smart-id"},
                "request_id": "request-2",
            },
            0,
        ),
        ({"code": 0, "data": {"smart_plus_ad_id": True}, "request_id": "request-2"}, 0),
    ],
)
def test_remote_rejection_or_missing_smart_id_preserves_safe_evidence(
    monkeypatch, response, code
):
    from app.modules.builds.sdk_requests import TikTokResponseError, invoke_create

    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        return HTTPResponse(body=json.dumps(response).encode(), status=200)

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(TikTokResponseError) as caught:
            invoke_create(
                client, kind="ad", body={**fixed("ad"), "operation_status": "ENABLE"}
            )
    assert caught.value.remote_code == code and caught.value.request_id == "request-2"
    assert "fixture-token-secret" not in str(caught.value)
    assert len(calls) == 1


def test_create_never_sends_non_enabled_status(monkeypatch):
    from app.modules.builds.sdk_requests import invoke_create

    calls = []
    monkeypatch.setattr("urllib3.PoolManager.request", lambda *a, **k: calls.append(1))
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(DomainError) as caught:
            invoke_create(
                client, kind="ad", body={**fixed("ad"), "operation_status": "DISABLE"}
            )
    assert caught.value.code == "invalid_creation_status" and not calls


def test_transport_timeout_is_unknown_and_not_retried(monkeypatch):
    from urllib3.exceptions import ReadTimeoutError

    from app.modules.builds.sdk_requests import TikTokResponseError, invoke_create

    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        raise ReadTimeoutError(None, "https://private/token", "fixture-token-secret")

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(TikTokResponseError) as caught:
            invoke_create(
                client,
                kind="campaign",
                body={**fixed("campaign"), "operation_status": "ENABLE"},
            )
    assert caught.value.code == "create_result_unknown"
    assert caught.value.remote_code == -1 and caught.value.request_id is None
    assert "fixture-token-secret" not in str(caught.value) and "private" not in str(
        caught.value
    )
    assert len(calls) == 1


def test_future_wait_does_not_return_or_cleanup_while_transport_runs(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.modules.builds.sdk_requests import invoke_create

    entered, release, cleaned = Event(), Event(), Event()

    def request(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return HTTPResponse(
            body=b'{"code":0,"data":{"campaign_id":"remote"}}', status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)

    def call():
        with official_client(access_token="fixture-token") as client:
            result = invoke_create(
                client,
                kind="campaign",
                body={**fixed("campaign"), "operation_status": "ENABLE"},
            )
        cleaned.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call)
        try:
            assert entered.wait(5)
            assert not future.done() and not cleaned.is_set()
        finally:
            release.set()
        assert future.result(timeout=5).remote_id == "remote"
    assert cleaned.is_set()


def test_fake_stores_all_layers_filters_account_and_forbids_status_update(monkeypatch):
    from app.modules.builds.sdk_requests import compile_request, invoke_create
    from tests.fakes.tiktok import FakeTikTokAPI

    fake = FakeTikTokAPI()
    with official_client(access_token="fixture-token") as client:
        monkeypatch.setattr(client, "call_api", fake.call_api)
        for kind in ("campaign", "adgroup", "ad"):
            assert (
                invoke_create(
                    client,
                    kind=kind,
                    body=compile_request(kind, fixed=fixed(kind), resolved={}),
                ).remote_id
                == "900000"
            )
        result = sdk.AdApi(client).smart_plus_ad_get(
            "fixture-account", "fixture-token", page=1, page_size=1
        )
        assert result.data["page_info"]["total_number"] == 1
        assert result.data["list"][0]["smart_plus_ad_id"] == "900000"
        empty = sdk.AdApi(client).smart_plus_ad_get("other-account", "fixture-token")
        assert empty.data["list"] == []
    with pytest.raises(AssertionError):
        fake.call_api(
            "/open_api/v1.3/smart_plus/ad/status/update/",
            "POST",
            body={"operation_status": "DISABLE"},
        )
    with pytest.raises(AssertionError):
        fake.call_api("/open_api/v1.3/minis/get/", "GET")
