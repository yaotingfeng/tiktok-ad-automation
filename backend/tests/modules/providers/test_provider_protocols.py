import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.jiashu import JiashuClient
from app.modules.providers.adapters.wangyan import WangyanClient


def test_jiashu_sessions_and_application_params_stay_request_local():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200, json={"code": "0000", "data": {"data": [], "count": 0}}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        a = JiashuClient(http, session="private-session-a", application_id="app-a")
        b = JiashuClient(http, session="private-session-b", application_id="app-b")
        assert a.search("Moon", 1) == {
            "items": [],
            "next_cursor": None,
            "complete": True,
        }
        b.search("Sun", 1)
        assert "session" not in http.headers
    assert [(r.headers["session"], r.url.params["channel"]) for r in seen] == [
        ("private-session-a", "app-a"),
        ("private-session-b", "app-b"),
    ]
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/Oversea/Video/getVideoList"
    assert dict(seen[0].url.params) == {
        "channel": "app-a",
        "channel_from": "7",
        "channel_type": "1",
        "site_type": "oversea_video_iaa",
    }
    assert json.loads(seen[0].content) == {
        "keywords": "Moon",
        "page": 1,
        "page_size": 20,
    }


@pytest.mark.parametrize(
    "code,want",
    [
        ("10001", "provider_session_expired"),
        ("10005", "provider_application_forbidden"),
        ("1234", "provider_rejected"),
    ],
)
def test_remote_errors_are_stable_and_do_not_retry_or_leak(code, want, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"code": code, "message": "secret-session-in-error", "data": {}}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = JiashuClient(
            http, session="secret-session-in-error", application_id="app"
        )
        with pytest.raises(DomainError) as error:
            client.search("Moon", 1)
    assert error.value.code == want and len(requests) == 1
    assert "secret-session-in-error" not in str(error.value) + caplog.text


def test_jiashu_login_discovers_all_apps_and_their_own_prefixes():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == "/User/login":
            assert json.loads(request.content) == {
                "username": "fixture-user",
                "password": "fixture-password",
            }
            assert request.url.params["site_type"] == "video"
            data = {"session": "fixture-session", "userName": "Do not persist"}
        elif request.url.path.endswith("getAppSwitchList"):
            assert request.url.params["channel"] == ""
            assert json.loads(request.content) == {"type": 1}
            data = [
                {"appid": "app-a", "name": "Alpha"},
                {"appid": "app-b", "name": "Beta"},
            ]
        else:
            assert request.url.path.endswith("getOptions")
            data = {"channel_prefix": request.url.params["channel"] + "_"}
        return httpx.Response(200, json={"code": "0000", "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = JiashuClient.login(
            http, username="fixture-user", password="fixture-password"
        )
        assert client.discover_applications() == [
            {
                "external_id": "app-a",
                "name": "Alpha",
                "channel_config": {"channel_prefix": "app-a_"},
                "tiktok_minis_id": None,
            },
            {
                "external_id": "app-b",
                "name": "Beta",
                "channel_config": {"channel_prefix": "app-b_"},
                "tiktok_minis_id": None,
            },
        ]
    assert len(seen) == 4


def test_missing_prefix_and_pagination_evidence_fail_closed():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "0000", "data": {"data": []}})
        )
    ) as http:
        client = JiashuClient(http, session="fake", application_id="app")
        with pytest.raises(DomainError) as error:
            client.search("Moon", 1)
        assert error.value.code == "provider_schema_unsupported"
        with pytest.raises(DomainError) as error:
            client.find_existing("drama", {}, None)
        assert error.value.code == "provider_channel_prefix_missing"


def test_channel_lookup_is_exact_and_retains_next_page():
    def handle(request):
        assert request.url.path.endswith("getChannelList")
        assert json.loads(request.content) == {
            "page": 1,
            "page_size": 20,
            "channel": "fixture_drama",
            "customer_id": "",
            "remark": "",
        }
        return httpx.Response(
            200,
            json={
                "code": "0000",
                "data": {
                    "data": [{"channel": "fixture_drama-other", "remark": "No"}] * 20,
                    "count": 21,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        result = client.find_existing("drama", {"episode": 1}, None)
    assert result == {"items": [], "next_cursor": "2", "complete": False}


def test_jiashu_save_rejects_conflict_before_remote_call_and_preserves_url():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"code": "0000", "data": True})

    payload = {
        "channel": "fixture_drama",
        "vid": "drama",
        "drama_num": 2,
        "jump_url": "https://www.tiktok.com/minis/example?channel=fixture_drama&vid=drama&dramaNum=2&charge_level=medium",
        "minis_path": "unaltered",
        "existing_config": {"drama_num": 1, "jump_url": "https://example.test/old"},
    }
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError) as error:
            client.create_step("save", payload)
        assert error.value.code == "config_conflict" and not seen
        payload["existing_config"] = {}
        assert client.create_step("save", payload) == {"accepted": True}
    sent = json.loads(seen[0].content)
    assert sent == {
        key: value for key, value in payload.items() if key != "existing_config"
    }


@pytest.mark.parametrize(
    "write,want,retryable",
    [(False, "provider_unavailable", True), (True, "provider_result_unknown", False)],
)
def test_transport_failure_distinguishes_reads_from_unknown_writes(
    write, want, retryable
):
    def handle(request):
        raise httpx.ReadTimeout("raw-private-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError) as error:
            if write:
                client.create_step(
                    "create",
                    {"channel": "fixture_drama", "vid": "drama", "remark": "Fixture"},
                )
            else:
                client.search("Moon", 1)
    assert error.value.code == want and error.value.retryable is retryable
    assert "raw-private-secret" not in str(error.value)


def test_wangyan_cookie_login_search_and_unverified_contracts():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("pwd_login"):
            assert json.loads(request.content) == {
                "email": "fixture@example.test",
                "password": "fixture-password",
            }
            return httpx.Response(
                200,
                json={"code": 0, "data": {}},
                headers={"set-cookie": "x-ds-admin-token=fake-token; Path=/; Secure"},
            )
        assert request.headers["cookie"] == "x-ds-admin-token=fake-token"
        assert request.url.path == "/api/distribute_admin/drama/list"
        assert dict(request.url.params) == {
            "app": "fixture.app",
            "page": "1",
            "page_size": "20",
            "title": "Moon",
        }
        return httpx.Response(
            200,
            json={"code": 0, "data": [{"id": "drama", "title": "Moon", "lang": "en"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WangyanClient.login(
            http,
            email="fixture@example.test",
            password="fixture-password",
            application_id="fixture.app",
        )
        assert client.search("Moon", 1)["items"] == [
            {"external_drama_id": "drama", "title": "Moon", "language": "en"}
        ]
        for method, args, expected in [
            (client.find_existing, ("drama", {}, None), "lookup_incomplete"),
            (client.read_link, ("remote",), "lookup_incomplete"),
            (
                client.create_step,
                ("create", {"drama_id": "drama", "episode": 1}),
                "lookup_incomplete",
            ),
        ]:
            with pytest.raises(DomainError) as error:
                method(*args)
            assert error.value.code == expected
    assert len(seen) == 2


def test_read_link_preserves_attribution_and_excludes_unrelated_remote_fields():
    url = "https://www.tiktok.com/minis/fixture?channel=fixture_drama&vid=drama&dramaNum=2&charge_level=medium"

    def handle(request):
        assert request.url.path.endswith("getGuideUrl")
        assert json.loads(request.content) == {"channel": "fixture_drama"}
        return httpx.Response(
            200,
            json={
                "code": "0000",
                "data": {
                    "config": {
                        "vid": "drama",
                        "drama_num": 2,
                        "jump_url": url,
                        "minis_path": "unaltered",
                        "session": "never-return",
                    }
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = JiashuClient(http, session="fake", application_id="app").read_link(
            "fixture_drama"
        )
    assert result["url"] == url and result["protected_base"] == ""
    assert result["attribution"] == {
        "channel": "fixture_drama",
        "vid": "drama",
        "dramaNum": "2",
        "charge_level": "medium",
    }
    assert "never-return" not in json.dumps(result)


@pytest.mark.parametrize(
    "config",
    [
        {"episode": 0},
        {"episode": True},
        {"charge_level": "high"},
        {"mode": "unverified"},
    ],
)
def test_unverified_configuration_never_silently_discarded(config):
    seen = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (
                seen.append(r),
                httpx.Response(
                    200, json={"code": "0000", "data": {"data": [], "count": 0}}
                ),
            )[1]
        )
    ) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError):
            client.find_existing("drama", config, None)
    assert not seen


@pytest.mark.parametrize("data", [None, "unparseable", {"unexpected": "value"}])
def test_malformed_write_ack_is_result_unknown(data):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "0000", "data": data})
        )
    ) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError) as error:
            client.create_step(
                "create",
                {"channel": "fixture_drama", "vid": "drama", "remark": "Fixture"},
            )
    assert error.value.code == "provider_result_unknown" and not error.value.retryable


def test_wangyan_app_discovery_uses_public_source_contract_without_default_app():
    def handle(request):
        assert request.url.path == "/api/account/group/apps"
        assert request.method == "GET" and not request.url.query
        assert request.headers["cookie"] == "x-ds-admin-token=fixture"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {"package_name": "fixture.alpha", "name": "Alpha", "is_tt": 1},
                    {"package_name": "fixture.beta", "name": "Beta", "is_tt": 0},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = WangyanClient(
            http, token="fixture", application_id=""
        ).discover_applications()
    assert [row["external_id"] for row in result] == ["fixture.alpha", "fixture.beta"]
    assert result[0]["channel_config"] == {"is_tt": True}
    assert all(row["tiktok_minis_id"] is None for row in result)


def test_wangyan_rendering_uses_public_bundle_evidence_and_preserves_remote_name():
    from app.modules.providers.adapters.wangyan import render_attribution

    row = {"drama_int_id": 101, "id": 202, "chapter_index": 3}
    assert render_attribution(row, "Fixture Drama") == "{b101/s202/c3}-Fixture Drama"
    assert (
        render_attribution({**row, "campaign_name": "Remote Original"}, "Title")
        == "Remote Original"
    )
    with pytest.raises(DomainError) as error:
        render_attribution({"id": 202}, "Title")
    assert error.value.code == "attribution_contract_unverified"


@pytest.mark.parametrize("kind", ["jiashu", "wangyan"])
def test_sanitized_protocol_fixture_search_contract(kind):
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / f"{kind}-contract.json").read_text()
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=fixture["search"])
        )
    ) as http:
        client = (
            JiashuClient(http, session="fake", application_id="synthetic-app")
            if kind == "jiashu"
            else WangyanClient(
                http, token="fake", application_id="synthetic.application"
            )
        )
        result = client.search("Fixture Moon", 1)
    assert result["items"][0]["title"] == "Fixture Moon"
    assert result["items"][0]["external_drama_id"] == (
        "123" if kind == "jiashu" else "opaque-drama-id"
    )


def test_generate_malformed_path_is_unknown_after_send():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "code": "0000",
                    "data": {
                        "url": "https://www.tiktok.com/minis/fixture",
                        "minis_path": {"malformed": True},
                    },
                },
            )
        )
    ) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError) as error:
            client.create_step(
                "generate",
                {
                    "channel": "fixture_drama",
                    "vid": "drama",
                    "drama_num": 1,
                    "existing_config": {},
                },
            )
    assert error.value.code == "provider_result_unknown"


def test_debug_wire_logs_do_not_disclose_authentication(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("httpcore.http11")
    disabled = logger.disabled
    logger.disabled = False

    def handle(_request):
        logger.debug(
            "receive_response_headers.complete Set-Cookie private-auth-session"
        )
        return httpx.Response(
            200, json={"code": "0000", "data": {"data": [], "count": 0}}
        )

    try:
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            JiashuClient(
                http, session="private-auth-session", application_id="app"
            ).search("Moon", 1)
        assert "private-auth-session" not in caplog.text
    finally:
        logger.disabled = disabled


@pytest.mark.parametrize("body", [{}, {"data": True}, {"code": None, "data": True}])
def test_missing_business_code_after_write_is_unknown(body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as http:
        client = JiashuClient(
            http, session="fake", application_id="app", channel_prefix="fixture_"
        )
        with pytest.raises(DomainError) as error:
            client.create_step(
                "create",
                {"channel": "fixture_drama", "vid": "drama", "remark": "Fixture"},
            )
    assert error.value.code == "provider_result_unknown"
