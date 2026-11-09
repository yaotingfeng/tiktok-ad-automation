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
