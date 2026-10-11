"""官方只读请求被平台限流时必须进入延后重试，其他 HTTP 错误仍为终态。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from business_api_client.rest import ApiException

from app.core.errors import DomainError
from app.integrations.tiktok.official.accounts import OfficialReadRequests
from app.integrations.tiktok.sdk import AccountAdmissionDeferred


@contextmanager
def _scope(_advertiser_id, _operation, _deadline):
    yield


def _requests() -> OfficialReadRequests:
    client = SimpleNamespace(deserialize=lambda response, response_type: response)
    return OfficialReadRequests(
        client,
        request_scope=_scope,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _http_error(status: int, headers: dict[str, str] | None = None) -> ApiException:
    response = SimpleNamespace(
        status=status, reason="", data=b"", getheaders=lambda: headers or {}
    )
    return ApiException(http_resp=response)


def _raise(error: Exception):
    def send(_timeout):
        raise error

    return send


@pytest.mark.parametrize(
    ("headers", "expected_ms"),
    [
        ({"Retry-After": "7"}, 7_000),
        # 平台未给出或给出无效等待时使用默认值；超大值被限制，避免任务长期挂起。
        ({}, 5_000),
        ({"Retry-After": "soon"}, 5_000),
        ({"Retry-After": "3600"}, 60_000),
        ({"Retry-After": "0"}, 1_000),
    ],
)
def test_throttled_read_is_deferred_with_bounded_retry_after(headers, expected_ms):
    with pytest.raises(AccountAdmissionDeferred) as caught:
        _requests().invoke(
            "accounts.bc_asset_get", None, _raise(_http_error(429, headers))
        )
    assert caught.value.code == "admission_deferred"
    assert caught.value.retryable is True
    assert caught.value.retry_after_ms == expected_ms


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
def test_other_http_failures_remain_terminal_response_errors(status):
    with pytest.raises(DomainError) as caught:
        _requests().invoke("accounts.bc_asset_get", None, _raise(_http_error(status)))
    assert not isinstance(caught.value, AccountAdmissionDeferred)
    assert caught.value.code == "tiktok_response_error"


@pytest.mark.parametrize(
    "message", ["Too many requests (QPM)", "QPD exceeded", "Requests PER DAY exceeded"]
)
def test_business_code_40100_defers_instead_of_failing(message):
    import json

    from business_api_client import ApiClient

    client = ApiClient()
    body = json.dumps(
        {"code": 40100, "message": message, "request_id": "r", "data": {}}
    )
    requests = OfficialReadRequests(
        client, request_scope=_scope, deadline=datetime.now(UTC) + timedelta(seconds=30)
    )
    before = datetime.now(UTC)
    with pytest.raises(AccountAdmissionDeferred) as caught:
        requests.invoke(
            "reports.integrated",
            "adv",
            lambda _timeout: client.deserialize(SimpleNamespace(data=body), "object"),
            exact_numbers=True,
        )
    after = datetime.now(UTC)
    if "QPM" in message:
        assert caught.value.retry_after_ms == 300_000
    else:
        # 跨日额度在次日 UTC 零点后留五秒余量，不能误用每分钟的五分钟等待。
        target = (before + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        assert (
            int((target - after).total_seconds() * 1000)
            <= caught.value.retry_after_ms
            <= int((target - before).total_seconds() * 1000)
        )
