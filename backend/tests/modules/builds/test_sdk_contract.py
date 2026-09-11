"""Pinned official SDK serialization, synthetic IDs, no external service calls."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.sdk import official_client
from app.modules.builds.request_compiler import decode_intent
from tests.contracts.test_tiktok_build_contract import build_bodies
from tests.integrations.tiktok.build_wire import sdk_build_operations


def fixed(kind):
    body = build_bodies()[kind.upper()]
    if kind == "campaign":
        body["budget"] = 100
    if kind == "adgroup":
        body["roas_bid"] = 1.5
        body["minis_id"] = "fixture-minis"
    return body


@pytest.mark.parametrize("kind", ["campaign", "adgroup", "ad"])
def test_compiler_preserves_fixed_intent_and_enables_every_layer(kind):
    from app.modules.builds.request_compiler import compile_request

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
    from app.modules.builds.request_compiler import compile_request

    with pytest.raises(DomainError) as caught:
        compile_request(
            "campaign", fixed=fixed("campaign"), resolved={key: "untrusted"}
        )
    assert caught.value.code == "scene_overrides_frozen_fields"


def test_group_never_gets_budget_and_unknown_kind_is_rejected():
    from app.modules.builds.request_compiler import compile_request

    with pytest.raises(DomainError) as caught:
        compile_request(
            "adgroup", fixed={**fixed("adgroup"), "budget": 50}, resolved={}
        )
    assert caught.value.code == "adgroup_budget_not_allowed"
    with pytest.raises(DomainError) as caught:
        compile_request("activate", fixed={}, resolved={})
    assert caught.value.code == "invalid_build_kind"


def test_official_method_accepts_unmodeled_dictionary(monkeypatch):
    import business_api_client as sdk

    calls = []

    def request(_pool, method, url, **kwargs):
        calls.append((method, url, json.loads(kwargs["body"])))
        return HTTPResponse(
            body=b'{"code":0,"data":{"adgroup_id":"group-1"}}', status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        sdk.AdgroupApi(client).smart_plus_adgroup_create(
            "fixture-token",
            body={
                "advertiser_id": "fixture-account",
                "operation_status": "ENABLE",
                "documented_extra": "kept",
                "minis_id": "fixture-minis",
            },
        )
    assert calls[0][1].endswith("/smart_plus/adgroup/create/")
    assert calls[0][2]["documented_extra"] == "kept"
    assert calls[0][2]["minis_id"] == "fixture-minis"


@pytest.mark.parametrize(
    "kind,id_key",
    [
        ("campaign", "campaign_id"),
        ("adgroup", "adgroup_id"),
        ("ad", "smart_plus_ad_id"),
    ],
)
def test_actual_official_transport_serializes_each_layer(monkeypatch, kind, id_key):
    from app.modules.builds.request_compiler import compile_request

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
        result = sdk_build_operations(client).create(
            attempt_id=uuid4(), intent=decode_intent(kind.upper(), body)
        )
    assert result.remote_id == "remote-1" and result.evidence.request_id == "request-1"
    assert result.operation_status == "ENABLE"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith(f"/smart_plus/{kind}/create/")
    assert json.loads(kwargs["body"]) == body
    assert kwargs["headers"]["Access-Token"] == "fixture-token"


@pytest.mark.parametrize(
    "response,_code",
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
    monkeypatch, response, _code
):

    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        return HTTPResponse(body=json.dumps(response).encode(), status=200)

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(RemoteCallError) as caught:
            sdk_build_operations(client).create(
                attempt_id=uuid4(), intent=decode_intent("AD", fixed("ad"))
            )
    assert (
        caught.value.effect == "UNKNOWN"
        and caught.value.evidence.request_id == "request-2"
    )
    assert "fixture-token-secret" not in str(caught.value)
    assert len(calls) == 1


def test_create_never_sends_non_enabled_status(monkeypatch):

    calls = []
    monkeypatch.setattr("urllib3.PoolManager.request", lambda *a, **k: calls.append(1))
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(ValidationError):
            sdk_build_operations(client).create(
                attempt_id=uuid4(),
                intent=decode_intent(
                    "AD", {**fixed("ad"), "operation_status": "DISABLE"}
                ),
            )
    assert not calls


def test_transport_timeout_is_unknown_and_not_retried(monkeypatch):
    from urllib3.exceptions import ReadTimeoutError

    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        raise ReadTimeoutError(None, "https://private/token", "fixture-token-secret")

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    with official_client(access_token="fixture-token") as client:
        with pytest.raises(RemoteCallError) as caught:
            sdk_build_operations(client).create(
                attempt_id=uuid4(), intent=decode_intent("CAMPAIGN", fixed("campaign"))
            )
    assert caught.value.code == "create_result_unknown"
    assert caught.value.effect == "UNKNOWN" and caught.value.evidence.request_id is None
    assert "fixture-token-secret" not in str(caught.value) and "private" not in str(
        caught.value
    )
    assert len(calls) == 1


def test_future_wait_does_not_return_or_cleanup_while_transport_runs(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

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
            result = sdk_build_operations(client).create(
                attempt_id=uuid4(), intent=decode_intent("CAMPAIGN", fixed("campaign"))
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
