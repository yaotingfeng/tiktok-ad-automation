import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.gangganhao import GangganhaoClient


def test_gangganhao_login_apps_search_and_iaa_create():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/login"):
            assert json.loads(request.content) == {
                "id": 56,
                "name": "portal-user",
                "password": "private-password",
            }
            return httpx.Response(200, json={"code": 0, "data": {"token": "portal-jwt"}})
        assert request.headers["authorization"] == "Bearer portal-jwt"
        if request.url.path.endswith("/apps"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"list": [{"authorizerAppId": 16, "appName": "Rovela", "deliveryMode": "iaa"}]},
                },
            )
        if request.url.path.endswith("/series"):
            assert request.url.params["keyword"] == "Moon"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [{"publishId": 886, "seriesId": 12, "seriesTitle": "Moon", "authorizerAppId": 16, "episodeCount": 10, "freeEpisodeCount": 3, "deliveryMode": "iaa"}],
                        "total": 1,
                    },
                },
            )
        if request.url.path.endswith("/campaign-link"):
            assert json.loads(request.content) == {
                "authorizerAppId": 16,
                "platformPublishId": 886,
                "seriesId": 12,
                "seriesTitle": "Moon",
                "episodeSeq": 1,
                "freeEpisodeCount": 3,
            }
            return httpx.Response(
                200,
                json={"code": 0, "data": {"id": 972, "linkCode": "C-1", "minisLink": "https://www.tiktok.com/minis/ggh"}},
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = GangganhaoClient.login(http, portal_id=56, username="portal-user", password="private-password")
        assert client.discover_applications() == [
            {
                "external_id": "16",
                "name": "Rovela",
                "channel_config": {"delivery_mode": "iaa"},
                "tiktok_minis_id": None,
            }
        ]
        page = client.search("Moon", None)
        assert page.items[0].external_drama_id == "886"
        receipt = client.create_link(
            "886",
            {"authorizer_app_id": "16", "series_id": "12", "series_title": "Moon", "free_episode_count": 3, "episode_seq": 1, "delivery_mode": "iaa"},
        )
    assert receipt.remote_id == "972"
    assert receipt.attribution == {"linkCode": "C-1", "name": ""}
    assert len(seen) == 4


def test_gangganhao_iap_requires_payment_template_before_any_write():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no remote call"))) as http:
        client = GangganhaoClient(http, token="portal-jwt")
        with pytest.raises(DomainError) as error:
            client.create_link(
                "886",
                {
                    "authorizer_app_id": "16",
                    "series_id": "12",
                    "series_title": "Moon",
                    "free_episode_count": 3,
                    "episode_seq": 1,
                    "delivery_mode": "mixed",
                },
            )
    assert error.value.code == "provider_request_invalid"


def test_gangganhao_lookup_compares_template_and_sequence():
    def handle(request):
        if request.url.path.endswith("/campaign-links"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"id": 972, "seriesId": 12}], "total": 1}},
            )
        if request.url.path.endswith("/campaign-links/972"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"id": 972, "seriesId": 12, "freeEpisodeCount": 3, "episodeSeq": 1, "paymentTemplateId": 7, "minisLink": "https://www.tiktok.com/minis/ggh"}},
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = GangganhaoClient(http, token="portal-jwt")
        page = client.lookup_link(
            "886",
            {"series_id": "12", "free_episode_count": 3, "episode_seq": 1, "payment_template_id": 7},
            None,
        )
    assert len(page.items) == 1
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = GangganhaoClient(http, token="portal-jwt")
        page = client.lookup_link(
            "886",
            {"series_id": "12", "free_episode_count": 3, "episode_seq": 1, "payment_template_id": 8},
            None,
        )
    assert page.items == []
