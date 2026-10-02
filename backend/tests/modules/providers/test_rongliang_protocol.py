import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.rongliang import RongliangClient


def test_rongliang_form_login_form_data_search_and_strict_create_readback():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/auth/login"):
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert request.content == b"email=user%40example.test&password=private-password"
            return httpx.Response(
                200,
                json={"code": 0, "token": "dist-jwt", "userName": "User"},
                headers={"set-cookie": "dist_token=dist-jwt; Path=/"},
            )
        assert request.headers["cookie"] == "dist_token=dist-jwt"
        if request.url.path.endswith("/link/form_data"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"packageOptions": [{"clientId": 1144, "clientName": "Cove", "clientType": "TIKTOK_MINI_PROGRAM"}]}},
            )
        if request.url.path.endswith("/compilations/page"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"records": [{"compilationsId": 8453, "originalTitle": "Moon", "compilationsName": "Moon Show"}], "total": 1}},
            )
        if request.url.path.endswith("/episodic_dramas"):
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"episodicDramaId": 991, "title": "1"}]},
            )
        if request.url.path.endswith("/link/page"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"records": [{"batchId": "B-1", "clientId": 1144, "compilationsId": 8453, "episodicDramaId": 991, "deliverPlatform": 1}], "total": 1}},
            )
        if request.url.path.endswith("/link/create"):
            assert json.loads(request.content) == {
                "clientId": 1144,
                "deliverPlatform": 1,
                "deliveryType": 1,
                "linkType": 2,
                "compilationsId": 8453,
                "compilationsAlias": "Moon",
                "episodicDramaId": 991,
            }
            return httpx.Response(200, json={"code": 0, "data": True})
        if request.url.path.endswith("/link/url"):
            assert request.url.params["batchId"] == "B-1"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"deepLink": "https://www.tiktok.com/minis/rl?batchId=B-1", "planName": "Moon", "adGroupName": "Moon Ads"}},
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient.login(http, email="user@example.test", password="private-password")
        assert client.discover_applications() == [
            {
                "external_id": "1144",
                "name": "Cove",
                "channel_config": {"platform": 1},
                "tiktok_minis_id": None,
            }
        ]
        page = client.search("Moon", None)
        assert page.items[0].external_drama_id == "8453"
        receipt = client.create_link(
            "8453",
            {
                "client_id": "1144",
                "episodic_drama_id": "991",
                "platform": 1,
                "delivery_type": 1,
                "link_type": 2,
                "alias": "Moon",
            },
        )
    assert receipt.remote_id == "B-1"
    assert receipt.protected_base == "Moon"
    assert receipt.attribution["adGroupName"] == "Moon Ads"
    assert len(seen) == 6


def test_rongliang_create_without_matching_batch_is_result_unknown():
    def handle(request):
        if request.url.path.endswith("/link/create"):
            return httpx.Response(200, json={"code": 0, "data": True})
        if request.url.path.endswith("/link/page"):
            return httpx.Response(200, json={"code": 0, "data": {"records": [], "total": 0}})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient(http, token="dist-jwt")
        with pytest.raises(DomainError) as error:
            client.create_link(
                "8453",
                {"client_id": "1144", "episodic_drama_id": "991", "platform": 1},
            )
    assert error.value.code == "provider_result_unknown"


def test_rongliang_token_expiry_is_stable():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"code": "user-2", "msg": "expired"}))
    ) as http:
        client = RongliangClient(http, token="expired")
        with pytest.raises(DomainError) as error:
            client.discover_applications()
    assert error.value.code == "provider_session_expired"
