from datetime import UTC, datetime

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_change_log_reader import SdkChangeLogReader


def test_reader_strict_contract_and_arguments():
    calls = []
    responses = [
        {"task_id": "export-test"},
        {"status": "PROCESSING"},
        {"status": "SUCCESS"},
        {"status": "SUCCESS", "changelog": "csv"},
    ]

    def send(operation, advertiser, arguments):
        calls.append((operation, advertiser, arguments))
        return responses.pop(0)

    reader = SdkChangeLogReader(send=send)
    assert (
        reader.change_log_create(
            advertiser_id="account-test",
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 11, 2, tzinfo=UTC),
        )
        == "export-test"
    )
    assert (
        reader.change_log_check(advertiser_id="account-test", task_id="export-test")
        == "PROCESSING"
    )
    assert (
        reader.change_log_check(advertiser_id="account-test", task_id="export-test")
        == "SUCCESS"
    )
    assert (
        reader.change_log_download(advertiser_id="account-test", task_id="export-test")
        == "csv"
    )
    assert calls[0][2]["timezone"] == "UTC"
    assert [r[0] for r in calls] == [
        "changelog.create",
        "changelog.check",
        "changelog.check",
        "changelog.download",
    ]


@pytest.mark.parametrize("response", [{}, {"status": "garbage"}, {"status": "SUCCESS"}])
def test_incomplete_download_is_not_empty(response):
    with pytest.raises(DomainError):
        SdkChangeLogReader(send=lambda *args: response).change_log_download(
            advertiser_id="test", task_id="test"
        )


def test_sdk_size_guard_preserves_log_split_signal(monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    from urllib3.response import HTTPResponse

    from app.integrations.tiktok.gateway import _sdk_report_send
    from app.integrations.tiktok.sdk import official_client

    @contextmanager
    def scope(*_args):
        yield

    def wire(*_args, **_kwargs):
        return HTTPResponse(
            body=b'{"code":0,"data":{"status":"SUCCESS","changelog":"'
            + b"x" * (8 * 1024 * 1024)
            + b'"}}',
            status=200,
        )

    monkeypatch.setattr("urllib3.PoolManager.request", wire)
    with official_client(access_token="synthetic") as client:
        reader = SdkChangeLogReader(
            send=_sdk_report_send(
                client, scope, datetime.now(UTC) + timedelta(seconds=30)
            )
        )
        with pytest.raises(DomainError) as caught:
            reader.change_log_download(advertiser_id="test", task_id="test")
        assert caught.value.code == "operation_log_export_too_large"
        assert reader.request_count == 1


@pytest.mark.parametrize(
    "operation,code,message",
    [
        ("changelog.check", 40007, "Resource not exist."),
        ("changelog.download", 40007, "Resource not exist."),
        ("changelog.download", 40000, "Service Error: Object Not Exist."),
    ],
)
def test_missing_export_is_recoverable_before_sdk_discards_provider_code(
    operation, code, message
):
    from contextlib import contextmanager
    from datetime import timedelta
    from types import SimpleNamespace

    from business_api_client import ApiClient

    from app.integrations.tiktok.official.accounts import OfficialReadRequests

    @contextmanager
    def scope(*_args):
        yield

    client = ApiClient()
    requests = OfficialReadRequests(
        client, request_scope=scope, deadline=datetime.now(UTC) + timedelta(seconds=30)
    )
    import json

    response = SimpleNamespace(
        data=json.dumps({"code": code, "message": message, "data": {}}).encode()
    )
    with pytest.raises(DomainError) as caught:
        requests.invoke(
            operation, "test", lambda _timeout: client.deserialize(response, "object")
        )
    assert caught.value.code == "operation_log_task_unavailable"


def test_other_provider_errors_are_not_misclassified_as_missing_export():
    from types import SimpleNamespace

    from app.integrations.tiktok.official.accounts import _strict_sdk_envelope

    def original(_response, _type):
        raise DomainError("original", "synthetic")

    for operation, code, message in [
        ("ads.get_ads", 40007, "Resource not exist."),
        ("changelog.create", 40007, "Resource not exist."),
        ("changelog.check", 40007, "Permission denied"),
        ("changelog.check", 40002, "Resource not exist."),
        ("changelog.check", 40000, "Service Error: Object Not Exist."),
        ("changelog.create", 40000, "Service Error: Object Not Exist."),
        ("changelog.download", 40000, "Invalid parameter"),
    ]:
        import json

        client = SimpleNamespace(deserialize=original)
        response = SimpleNamespace(
            data=json.dumps({"code": code, "message": message, "data": {}}).encode()
        )
        with _strict_sdk_envelope(client, operation=operation):
            with pytest.raises(DomainError) as caught:
                client.deserialize(response, "object")
        assert caught.value.code == "original"
