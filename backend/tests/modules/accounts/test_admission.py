from unittest.mock import Mock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from redis.exceptions import ConnectionError

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok import sdk
from app.jobs.admission import Admission, admission_keys


def test_denied_call_does_not_invoke_sdk_or_release(context, monkeypatch, policy):
    monkeypatch.setattr(sdk, "admit_call", Mock(return_value=Admission(False, 800)))
    release, remote = Mock(), Mock()
    monkeypatch.setattr(sdk, "release_call", release)
    with pytest.raises(sdk.AccountAdmissionDeferred) as error:
        with sdk.admitted_account_call(
            Mock(),
            context=context,
            endpoint="/bc/get/",
            advertiser_id="",
            policy=policy,
        ):
            remote()
    assert error.value.retry_after_ms == 800
    remote.assert_not_called()
    release.assert_not_called()


def test_redis_failure_prevents_request(context, monkeypatch, policy):
    monkeypatch.setattr(
        sdk,
        "admit_call",
        Mock(side_effect=DomainError("admission_unavailable", "offline", True)),
    )
    remote = Mock()
    with pytest.raises(DomainError):
        with sdk.admitted_account_call(
            Mock(),
            context=context,
            endpoint="/bc/get/",
            advertiser_id="",
            policy=policy,
        ):
            remote()
    remote.assert_not_called()


def test_each_actual_call_uses_app_scope_and_fresh_lease(context, monkeypatch, policy):
    admit = Mock(return_value=Admission(True, 0))
    release = Mock()
    monkeypatch.setattr(sdk, "admit_call", admit)
    monkeypatch.setattr(sdk, "release_call", release)
    for _ in range(2):
        with sdk.admitted_account_call(
            Mock(),
            context=context,
            endpoint="/bc/get/",
            advertiser_id="",
            policy=policy,
        ):
            pass
    arguments = [call.kwargs for call in admit.call_args_list]
    assert all(call["app_scope"] == settings.TIKTOK_APP_ID for call in arguments)
    assert arguments[0]["lease_id"] != arguments[1]["lease_id"]
    assert [call.kwargs for call in release.call_args_list] == [
        {key: value for key, value in call.items() if key != "policy"}
        for call in arguments
    ]


@pytest.mark.parametrize("raises", [True, False])
def test_lease_is_released_and_release_failure_preserves_result(
    context, monkeypatch, policy, raises, caplog
):
    monkeypatch.setattr(sdk, "admit_call", Mock(return_value=Admission(True, 0)))
    release = Mock(side_effect=ConnectionError("fake-secret"))
    monkeypatch.setattr(sdk, "release_call", release)

    def request():
        with sdk.admitted_account_call(
            Mock(),
            context=context,
            endpoint="/bc/get/?secret=fake-secret",
            advertiser_id="",
            policy=policy,
        ):
            if raises:
                raise RuntimeError("owned error")
            return "received"

    if raises:
        with pytest.raises(RuntimeError, match="owned error"):
            request()
    else:
        assert request() == "received"
    release.assert_called_once()
    assert "fake-secret" not in caplog.text


def test_real_redis_two_connections_share_app_quota(context, redis_client, policy):
    policy.app_max_inflight = 1
    endpoint = "/oauth/test/"
    try:
        with sdk.admitted_account_call(
            redis_client,
            context=context,
            endpoint=endpoint,
            advertiser_id="",
            policy=policy,
        ):
            with pytest.raises(sdk.AccountAdmissionDeferred):
                with sdk.admitted_account_call(
                    redis_client,
                    context=context,
                    endpoint="/bc/test/",
                    advertiser_id="other",
                    policy=policy,
                ):
                    pytest.fail("A second connection cannot evade application quota")
        keys = admission_keys(settings.TIKTOK_APP_ID, endpoint, context.tenant_id, "")
        assert all(redis_client.zcard(key) == 0 for key in keys[2:])
        assert redis_client.zcard(keys[0]) == 1
    finally:
        for name, advertiser in [(endpoint, ""), ("/bc/test/", "other")]:
            redis_client.delete(
                *admission_keys(
                    settings.TIKTOK_APP_ID, name, context.tenant_id, advertiser
                )
            )


def test_retry_after_http_header():
    import asyncio

    from app.core.errors import domain_error_handler

    response = asyncio.run(
        domain_error_handler(None, sdk.AccountAdmissionDeferred(1201))
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"


@pytest.mark.parametrize(
    "interrupt", [SystemExit, KeyboardInterrupt, SoftTimeLimitExceeded]
)
def test_interrupted_sdk_scope_retains_real_redis_lease(
    context, redis_client, policy, interrupt
):
    policy.app_max_inflight = 1
    endpoint = "/fixture/interrupted/get/"
    keys = admission_keys(settings.TIKTOK_APP_ID, endpoint, context.tenant_id, "a")
    try:
        with pytest.raises(interrupt):
            with sdk.admitted_account_call(
                redis_client,
                context=context,
                endpoint=endpoint,
                advertiser_id="a",
                policy=policy,
            ):
                raise interrupt()
        assert all(redis_client.zcard(key) == 1 for key in keys[2:])
        assert all(0 < redis_client.pttl(key) <= 120000 for key in keys[2:])
        with pytest.raises(sdk.AccountAdmissionDeferred):
            with sdk.admitted_account_call(
                redis_client,
                context=context,
                endpoint=endpoint,
                advertiser_id="a",
                policy=policy,
            ):
                pytest.fail("scope cleanup cannot release a terminating call")
    finally:
        redis_client.delete(*keys)
