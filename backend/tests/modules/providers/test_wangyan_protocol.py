"""Synthetic fixtures from public bundle/CLI shapes, never live receipts."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.providers.adapters.wangyan import WangyanClient


def row(identity=1, **changes):
    return {
        "id": identity,
        "app": "fixture.app",
        "drama_id": "opaque-drama",
        "drama_int_id": 501,
        "chapter_index": 2,
        "promote_platform": "tiktok",
        "promote_name": "stable-fixture-name",
        "tt_minis_link": "https://www.tiktok.com/minis/fixture?spread_id=1",
        **changes,
    }


def client(handler):
    return WangyanClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        token="private-fixture-token",
        application_id="fixture.app",
    )


def reply(rows, total=None):
    return httpx.Response(
        200,
        json={"code": 0, "data": rows, "total": len(rows) if total is None else total},
    )


def test_full_history_cursor_preserves_dates_total_and_every_observed_identity():
    requests = []

    def transport(request):
        requests.append(request)
        return (
            reply([row(i) for i in range(1, 21)], 21)
            if request.url.params["page"] == "1"
            else reply([row(21)], 21)
        )

    api = client(transport)
    first = api.find_existing("opaque-drama", {"episode": 2}, None)
    assert first["complete"] is False and len(first["items"]) == 20
    assert first["observed_ids"] == [str(i) for i in range(1, 21)]
    last = api.find_existing("opaque-drama", {"episode": 2}, first["next_cursor"])
    assert (
        last["complete"] is True and last["next_cursor"] is None and last["total"] == 21
    )
    assert last["items"][0]["remote_id"] == "21"
    for request in requests:
        assert (
            request.method == "GET"
            and request.url.path == "/api/distribute_admin/promote/link/list"
        )
        assert request.headers["cookie"] == "x-ds-admin-token=private-fixture-token"
        assert request.url.params["start"] == "1970-01-01"
        assert (
            request.url.params["end"]
            == (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
        )
        assert (
            request.url.params["app"] == "fixture.app"
            and request.url.params["drama_id"] == "opaque-drama"
        )
        assert request.url.params["page_size"] == "20"
    assert requests[0].url.params["end"] == requests[1].url.params["end"]


def test_candidates_require_exact_scope_but_observation_covers_filtered_rows():
    api = client(
        lambda request: reply(
            [
                row(1),
                row(2, app="other.app"),
                row(3, drama_id="other-drama"),
                row(4, promote_platform="facebook"),
                row(5, chapter_index=1),
            ]
        )
    )
    result = api.find_existing("opaque-drama", {"episode": 2}, None)
    assert [item["remote_id"] for item in result["items"]] == ["1"]
    assert (
        result["observed_ids"] == ["1", "2", "3", "4", "5"]
        and result["complete"] is True
    )
    assert result["items"][0]["promote_name"] == "stable-fixture-name"


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "data": []},
        {"code": 0, "data": [], "total": True},
        {"code": 0, "data": [], "total": -1},
        {"code": 0, "data": [], "total": "0"},
        {"code": 0, "data": [row()], "total": 21},
        {"code": 0, "data": [row(), row()], "total": 2},
        {"code": 0, "data": [row(i + 1) for i in range(21)], "total": 21},
    ],
)
def test_incomplete_or_duplicate_pages_never_prove_absence(body):
    with pytest.raises(DomainError):
        client(lambda request: httpx.Response(200, json=body)).find_existing(
            "opaque-drama", {"episode": 2}, None
        )


@pytest.mark.parametrize(
    "missing", ["app", "drama_id", "chapter_index", "promote_platform"]
)
def test_missing_comparable_config_is_not_silently_filtered(missing):
    item = row()
    del item[missing]
    with pytest.raises(DomainError) as error:
        client(lambda request: reply([item])).find_existing(
            "opaque-drama", {"episode": 2}, None
        )
    assert error.value.code == "config_unverifiable"


def test_cursor_rejects_changed_scope_and_server_total():
    api = client(lambda request: reply([row(i + 1) for i in range(20)], 21))
    cursor = api.find_existing("opaque-drama", {"episode": 2}, None)["next_cursor"]
    for drama, config in [("other", {"episode": 2}), ("opaque-drama", {"episode": 3})]:
        with pytest.raises(DomainError):
            api.find_existing(drama, config, cursor)
    with pytest.raises(DomainError):
        client(lambda request: reply([row(21)], 22)).find_existing(
            "opaque-drama", {"episode": 2}, cursor
        )


def test_empty_complete_page_and_unknown_configuration():
    api = client(lambda request: reply([]))
    assert api.find_existing("opaque-drama", {}, None)["complete"] is True
    with pytest.raises(DomainError) as error:
        api.find_existing("opaque-drama", {"price": "invented"}, None)
    assert error.value.code == "config_unverifiable"


def test_exact_id_returns_remote_url_configuration_and_only_safe_projection():
    def transport(request):
        assert (
            request.url.params["id"] == "12"
            and request.url.params["app"] == "fixture.app"
        )
        assert request.url.params["start"] == "1970-01-01"
        return reply(
            [row(12, campaign_name="protected-remote", password="never-project")]
        )

    result = client(transport).read_link("12")
    assert result["remote_id"] == "12" and result["url"] == row()["tt_minis_link"]
    assert result["config"] == {
        "vid": "opaque-drama",
        "drama_num": 2,
        "jump_url": row()["tt_minis_link"],
    }
    assert (
        result["protected_base"] == "protected-remote"
        and result["attribution"]["drama_int_id"] == 501
    )
    assert "never-project" not in json.dumps(result)


@pytest.mark.parametrize(
    "rows,total",
    [([], 0), ([row(2)], 1), ([row(1), row(2)], 2), ([row(1, app="other")], 1)],
)
def test_exact_lookup_never_uses_neighbour_or_other_application(rows, total):
    with pytest.raises(DomainError):
        client(lambda request: reply(rows, total)).read_link("1")


@pytest.mark.parametrize(
    "changes",
    [
        {"tt_minis_link": []},
        {"tt_minis_link": "http://www.tiktok.com/a"},
        {"tt_minis_link": "https://evil.test/a"},
        {"campaign_name": False},
        {"drama_int_id": True},
    ],
)
def test_readback_rejects_unverifiable_link_and_attribution(changes):
    with pytest.raises(DomainError):
        client(lambda request: reply([row(**changes)])).read_link("1")


def test_create_uses_cli_shape_and_source_supported_stable_name():
    seen = []

    def transport(request):
        seen.append(request)
        assert (
            request.method == "POST"
            and request.url.path == "/api/distribute_admin/promote/link/create"
        )
        assert json.loads(request.content) == {
            "app": "fixture.app",
            "drama_id": "opaque-drama",
            "chapter_index": 2,
            "promote_platform": "tiktok",
            "promote_name": "operation-123",
        }
        return httpx.Response(
            200, json={"code": 0, "data": {"id": 12}, "msg": "private-response"}
        )

    assert client(transport).create_step(
        "create",
        {"vid": "opaque-drama", "drama_num": 2, "promote_name": "operation-123"},
    ) == {"accepted": True, "remote_id": "12"}
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body", [{"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": {}}]
)
def test_success_ack_without_id_remains_success_for_durable_readback(body):
    assert client(lambda request: httpx.Response(200, json=body)).create_step(
        "create", {"vid": "opaque", "drama_num": 1}
    ) == {"accepted": True}


@pytest.mark.parametrize(
    "body",
    [
        {"data": {}},
        {"code": False},
        {"code": 0, "data": {"id": False}},
        {"code": 0, "data": {"id": -1}},
        {"code": 0, "data": []},
    ],
)
def test_malformed_write_ack_is_unknown_without_retry(body):
    seen = []

    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={**body, "msg": "private-fixture-token"})

    with pytest.raises(DomainError) as error:
        client(transport).create_step("create", {"vid": "opaque", "drama_num": 1})
    assert (
        error.value.code == "provider_result_unknown" and error.value.retryable is False
    )
    assert "private-fixture-token" not in str(error.value) and len(seen) == 1


def test_write_timeout_never_replays_or_exposes_credentials():
    seen = []

    def transport(request):
        seen.append(request)
        raise httpx.ReadTimeout("private-fixture-token", request=request)

    with pytest.raises(DomainError) as error:
        client(transport).create_step("create", {"vid": "opaque", "drama_num": 1})
    assert (
        error.value.code == "provider_result_unknown" and error.value.retryable is False
    )
    assert len(seen) == 1 and "private-fixture-token" not in str(error.value)


def test_205_history_pages_remain_bounded_and_do_not_extend_end_date(monkeypatch):
    from app.modules.providers.adapters import wangyan

    calls, ids, cursor = [], [], None
    monkeypatch.setattr(wangyan, "_tomorrow", lambda: "2026-09-10")

    def transport(request):
        calls.append(request)
        offset = (int(request.url.params["page"]) - 1) * 20
        assert request.url.params["end"] == "2026-09-10"
        return reply(
            [row(i) for i in range(offset + 1, min(offset + 20, 205) + 1)], 205
        )

    api = client(transport)
    for page in range(1, 12):
        result = api.find_existing("opaque-drama", {"episode": 2}, cursor)
        ids.extend(result["observed_ids"])
        cursor = result["next_cursor"]
        if cursor:
            assert len(cursor) < 400
        if page == 1:
            monkeypatch.setattr(wangyan, "_tomorrow", lambda: "2026-09-11")
    assert ids == [str(i) for i in range(1, 206)] and len(calls) == 11
    assert result["complete"] is True and cursor is None


@pytest.mark.parametrize(
    "cursor", ["", "not-json", "e30=", "!invalidbase64!", "A" * 1025]
)
def test_malformed_cursors_cannot_reach_transport(cursor):
    def forbidden(_request):
        pytest.fail("invalid cursor performed network request")

    with pytest.raises(DomainError):
        client(forbidden).find_existing("opaque-drama", {"episode": 2}, cursor)


def test_engineering_history_cap_never_claims_complete(monkeypatch):
    from app.modules.providers.adapters import wangyan

    monkeypatch.setattr(wangyan, "MAX_HISTORY_PAGES", 1)
    with pytest.raises(DomainError) as error:
        client(
            lambda request: reply([row(i + 1) for i in range(20)], 21)
        ).find_existing("opaque-drama", {"episode": 2}, None)
    assert error.value.code == "lookup_incomplete"


def test_cross_page_duplicates_are_returned_for_durable_history_fence():
    def transport(request):
        return (
            reply([row(i + 1) for i in range(20)], 21)
            if request.url.params["page"] == "1"
            else reply([row(1)], 21)
        )

    api = client(transport)
    first = api.find_existing("opaque-drama", {"episode": 2}, None)
    last = api.find_existing("opaque-drama", {"episode": 2}, first["next_cursor"])
    assert set(first["observed_ids"]) & set(last["observed_ids"]) == {"1"}
    # complete is the page boundary only; the workflow must reject this history.


@pytest.mark.parametrize(
    "payload",
    [
        {"vid": "opaque", "drama_num": 1, "promote_name": " "},
        {"vid": "opaque", "drama_num": 1, "app": "other"},
        {"vid": "opaque", "drama_num": True},
        {"vid": "opaque", "drama_num": 1, "promote_name": "x" * 256},
    ],
)
def test_invalid_create_does_not_touch_transport(payload):
    def forbidden(_request):
        pytest.fail("invalid input performed network request")

    with pytest.raises(DomainError):
        client(forbidden).create_step("create", payload)


def test_empty_application_does_not_use_provider_default():
    def forbidden(_request):
        pytest.fail("missing app performed network request")

    api = client(forbidden)
    api.application_id = ""
    with pytest.raises(DomainError):
        api.find_existing("opaque-drama", {}, None)
    with pytest.raises(DomainError):
        api.create_step("create", {"vid": "opaque-drama", "drama_num": 1})
