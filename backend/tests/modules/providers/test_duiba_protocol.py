import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.duiba import DuibaClient


def test_duiba_login_apps_search_preview_and_create_are_normalized():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content) == {
                "account": "duiba-account",
                "password": "private-password",
            }
            return httpx.Response(200, json={"success": True, "data": {"token": "jwt"}})
        assert request.headers["authorization"] == "Bearer jwt"
        if request.url.path.endswith("/miniapp/myList"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": 77, "name": "English Mini", "language": "EN"}],
                },
            )
        if request.url.path.endswith("/drama/page"):
            assert request.url.params["keyword"] == "Moon"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "list": [
                            {"id": 12, "title": "Moon"},
                            {"id": 13, "title": "Moon Other"},
                        ],
                        "total": 2,
                    },
                },
            )
        if request.url.path.endswith("/drama/preview"):
            assert request.url.params["id"] == "12"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"serialNo": 1, "lc671EpisodeId": "ep-1"},
                        {"serialNo": 9, "lc671EpisodeId": "ep-9"},
                    ],
                },
            )
        if request.url.path.endswith("/link/create"):
            assert json.loads(request.content) == {
                "dramaId": "12",
                "miniappId": 77,
                "defaultEpisode": 1,
                "cardPointEpisode": 9,
                "defaultEpisodeLcId": "ep-1",
                "cardPointEpisodeLcId": "ep-9",
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "linkNo": "L-99",
                        "minisLink": "https://www.tiktok.com/minis/duiba?x=1",
                    },
                },
            )
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = DuibaClient.login(http, account="duiba-account", password="private-password")
        assert client.discover_applications() == [
            {
                "external_id": "77",
                "name": "English Mini",
                "channel_config": {"language": "EN"},
                "tiktok_minis_id": None,
            }
        ]
        page = client.search("Moon", None)
        assert [item.external_drama_id for item in page.items] == ["12"]
        assert page.complete
        assert client.preview_drama("12")[0]["lc671EpisodeId"] == "ep-1"
        receipt = client.create_link(
            "12",
            {
                "miniapp_id": "77",
                "episode": 1,
                "card_point_episode": 9,
                "default_episode_lc_id": "ep-1",
                "card_point_episode_lc_id": "ep-9",
            },
        )
    assert receipt.remote_id == "L-99"
    assert receipt.protected_base == ""
    assert receipt.attribution == {"linkNo": "L-99", "miniapp_id": "77"}
    assert len(seen) == 5


def test_duiba_link_page_500_is_a_retryable_lookup_failure_and_not_a_write_success():
    def handle(request):
        if request.url.path.endswith("/link/page"):
            return httpx.Response(500, text="temporary failure")
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = DuibaClient(http, token="jwt")
        with pytest.raises(DomainError) as error:
            client.lookup_link(
                "12",
                {"miniapp_id": "77", "episode": 1, "card_point_episode": 1},
                None,
            )
    assert error.value.code == "provider_unavailable"
    assert error.value.retryable


def test_duiba_rejects_missing_episode_lc_ids_before_remote_write():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no call"))) as http:
        client = DuibaClient(http, token="jwt")
        with pytest.raises(DomainError) as error:
            client.create_link(
                "12",
                {"miniapp_id": "77", "episode": 1, "card_point_episode": 9},
            )
    assert error.value.code == "provider_request_invalid"
