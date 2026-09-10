"""Pinned SDK and urllib3 multipart encoder; only final connection I/O is doubled."""

import json
import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import official_client
from app.modules.materials.sdk_assets import (
    RemoteCallBudget,
    parse_upload,
    read_source_preview,
    upload_video_url,
)

TOKEN = "offline-token-secret"
DIGEST = "abcdef0123456789abcdef0123456789"
URL = "https://approved-cdn.example/media.mp4?signature=offline-url-secret"
HOSTS = frozenset({"approved-cdn.example"})


@pytest.fixture
def budget():
    return RemoteCallBudget(
        deadline=datetime.now(UTC) + timedelta(seconds=50),
        hard_limit_seconds=50,
        lease_ms=60000,
    )


@pytest.fixture
def transport(monkeypatch):
    calls, responses = [], []

    def request(_pool, method, url, **kwargs):
        assert _pool.retries.total == 0 and _pool.retries.redirect == 0
        calls.append((method, url, kwargs))
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, HTTPResponse):
            # Real ConnectionPool attaches its effective retry policy to the
            # response; PoolManager uses it to reject cross-host redirects.
            value.retries = _pool.retries
            return value
        return HTTPResponse(body=json.dumps(value).encode(), status=200)

    # Leave FileApi, ApiClient, RESTClient, PoolManager and its multipart encoder
    # real. The patched connection method cannot open any external socket.
    monkeypatch.setattr("urllib3.connectionpool.HTTPSConnectionPool.urlopen", request)
    return calls, responses


def upload(client, budget, **kwargs):
    return upload_video_url(
        client,
        **{
            "advertiser_id": "actual-source-account",
            "remote_name": "stable-attempt-001.mp4",
            "video_url": URL,
            "md5": DIGEST,
            "budget": budget,
            **kwargs,
        },
    )


def read(client, budget, **kwargs):
    return read_source_preview(
        client,
        **{
            "advertiser_id": "actual-source-account",
            "video_id": "known-source-vid",
            "md5": DIGEST,
            "allowed_hosts": HOSTS,
            "budget": budget,
            **kwargs,
        },
    )


def info(**kwargs):
    return {
        "code": 0,
        "data": {
            "list": [
                {
                    "video_id": "known-source-vid",
                    "material_id": "actual-source-mid",
                    "signature": DIGEST,
                    "displayable": True,
                    "width": 1080,
                    "height": 1920,
                    "size": 12345,
                    "duration": 4.5,
                    "format": "mp4",
                    "preview_url": URL,
                    **kwargs,
                }
            ]
        },
    }


def multipart_fields(call):
    kwargs = call[2]
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {kwargs['headers']['Content-Type']}\r\n\r\n".encode()
        + kwargs["body"]
    )
    parts = list(message.iter_parts())
    assert all(part.get_filename() is None for part in parts)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(
            decode=True
        ).decode()
        for part in parts
    }


def test_real_multipart_has_url_and_digest_but_no_file_bytes(
    transport, budget, monkeypatch
):
    calls, responses = transport
    responses.append(
        {
            "code": 0,
            "request_id": "receipt-request",
            "data": [{"video_id": "returned-vid", "material_id": "returned-mid"}],
        }
    )

    def no_original_read(*_args, **_kwargs):
        pytest.fail("URL upload must not open a local original")

    with official_client(access_token=TOKEN) as client:
        with monkeypatch.context() as patch:
            patch.setattr("builtins.open", no_original_read)
            receipt = parse_upload(upload(client, budget, md5=DIGEST.upper()))
        assert receipt == {"video_id": "returned-vid", "mid": "returned-mid"}
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST" and url.endswith("/file/video/ad/upload/")
    assert multipart_fields(calls[0]) == {
        "advertiser_id": "actual-source-account",
        "auto_bind_enabled": "False",
        "auto_fix_enabled": "False",
        "file_name": "stable-attempt-001.mp4",
        "upload_type": "UPLOAD_BY_URL",
        "video_signature": DIGEST,
        "video_url": URL,
    }
    assert kwargs["headers"]["Access-Token"] == TOKEN
    assert kwargs["timeout"].connect_timeout + kwargs["timeout"].read_timeout <= 45
    assert "Access-Token" not in client.default_headers


@pytest.mark.parametrize("digest", [None, "", "a" * 31, "g" * 32, "a" * 32 + "-2", 42])
def test_invalid_trusted_digest_is_rejected_before_send(transport, budget, digest):
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError, match="素材缺少") as error:
            upload(client, budget, md5=digest)
    assert error.value.code == "material_digest_missing"
    assert transport[0] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"advertiser_id": 123},
        {"advertiser_id": " account "},
        {"remote_name": ""},
        {"remote_name": "a" * 101},
        {"remote_name": "name\nsecret"},
        {"video_url": "http://approved-cdn.example/file"},
        {"video_url": "https://user:pass@approved-cdn.example/file"},
        {"video_url": "https://127.0.0.1/file"},
    ],
)
def test_invalid_upload_request_is_rejected_without_network(transport, budget, kwargs):
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            upload(client, budget, **kwargs)
    assert error.value.code == "material_request_invalid"
    assert transport[0] == []


@pytest.mark.parametrize("operation", [upload, read])
@pytest.mark.parametrize(
    "override,code",
    [
        ({"deadline": datetime.now(UTC) - timedelta(seconds=1)}, "material_deadline"),
        ({"deadline": datetime.now(UTC) + timedelta(seconds=4)}, "material_deadline"),
        ({"deadline": datetime(2026, 1, 1)}, "admission_policy_invalid"),
        ({"hard_limit_seconds": 0}, "admission_policy_invalid"),
        ({"hard_limit_seconds": True}, "admission_policy_invalid"),
        ({"lease_ms": 55000}, "admission_policy_invalid"),
    ],
)
def test_actual_deadline_and_policy_are_required_before_any_network(
    transport, budget, operation, override, code
):
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            operation(client, replace(budget, **override))
    assert error.value.code == code
    assert transport[0] == []


def test_source_read_is_fresh_exact_and_does_not_fetch_media(transport, budget):
    calls, responses = transport
    next_url = URL.replace("offline-url-secret", "refreshed-url-secret")
    responses.extend([info(), info(preview_url=next_url, signature=DIGEST.upper())])
    with official_client(access_token=TOKEN) as client:
        first = read(client, budget)
        second = read(client, budget)
    assert first.url == URL and second.url == next_url
    assert first.video_id == "known-source-vid" and first.mid == "actual-source-mid"
    assert first.advertiser_id == "actual-source-account"
    assert first.md5 == DIGEST and first.displayable is True
    assert (first.width, first.height, first.size, first.duration, first.format) == (
        1080,
        1920,
        12345,
        4.5,
        "mp4",
    )
    assert URL not in repr(first) and DIGEST not in repr(first)
    assert len(calls) == 2
    from urllib.parse import parse_qs, urlsplit

    for method, url, kwargs in calls:
        assert method == "GET" and urlsplit(url).path.endswith("/file/video/ad/info/")
        query = parse_qs(urlsplit(url).query)
        assert query["advertiser_id"] == ["actual-source-account"]
        assert json.loads(query["video_ids"][0]) == ["known-source-vid"]
        assert kwargs["timeout"].read_timeout <= 30


@pytest.mark.parametrize(
    "override",
    [
        {"video_id": "other-vid"},
        {"video_id": None},
        {"signature": "a" * 32},
        {"signature": None},
        {"displayable": False},
        {"displayable": "true"},
        {"advertiser_id": "other-account"},
        {"material_id": "bad\nmid"},
        {"width": None},
        {"width": True},
        {"height": 0},
        {"size": 0},
        {"size": "123"},
        {"duration": None},
        {"duration": float("inf")},
        {"duration": 10**400},
        {"duration": True},
        {"format": None},
        {"format": "html"},
    ],
)
def test_mismatched_or_unusable_media_is_never_source_evidence(
    transport, budget, override
):
    transport[1].append(info(**override))
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            read(client, budget)
    assert error.value.code == "unsupported_material_schema"


@pytest.mark.parametrize("rows", [[], [info()["data"]["list"][0]] * 2, [None]])
def test_missing_ambiguous_or_invalid_rows_are_rejected(transport, budget, rows):
    transport[1].append({"code": 0, "data": {"list": rows}})
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            read(client, budget)
    assert error.value.code == "unsupported_material_schema"


@pytest.mark.parametrize(
    "field",
    [
        "video_id",
        "signature",
        "displayable",
        "width",
        "height",
        "size",
        "duration",
        "format",
        "preview_url",
    ],
)
def test_missing_required_source_fields_fail_closed(transport, budget, field):
    response = info()
    response["data"]["list"][0].pop(field)
    transport[1].append(response)
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError):
            read(client, budget)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://approved-cdn.example/a",
        "//approved-cdn.example/a",
        "https://@approved-cdn.example/a",
        "https://user:pass@approved-cdn.example/a",
        "https://approved-cdn.example.attacker.example/a",
        "https://other.example/a",
        "https://child.approved-cdn.example/a",
        "https://approved-cdn.example.:443/a",
        "https://approved-cdn.example:444/a",
        "https://approved-cdn.example:a/a",
        "https://approved-cdn.example:/a",
        "https://approved-cdn.example/a#secret",
        "https://approved-cdn.example/a#",
        "https://approved-cdn.example/\nsecret",
        "https://approved-cdn.example/\\secret",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://approved-cdn.example%2fattacker.example/a",
    ],
)
def test_preview_url_requires_exact_vetted_https_host(transport, budget, url):
    transport[1].append(info(preview_url=url))
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            read(client, budget)
    assert error.value.code == "material_preview_unverified"


@pytest.mark.parametrize(
    "hosts",
    [
        frozenset(),
        frozenset({"*.example"}),
        frozenset({"127.0.0.1"}),
        frozenset({"https://approved-cdn.example"}),
        frozenset({"Approved-cdn.example"}),
        frozenset({"localhost"}),
        {"approved-cdn.example"},
    ],
)
def test_missing_or_malformed_deployment_host_policy_fails_closed(
    transport, budget, hosts
):
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            read(client, budget, allowed_hosts=hosts)
    assert error.value.code == "material_preview_unverified"
    assert transport[0] == []


@pytest.mark.parametrize("operation", [upload, read])
@pytest.mark.parametrize(
    "failure", ["timeout", "http", "business", "malformed", "redirect"]
)
def test_failures_are_redacted_and_never_retried(
    transport, budget, caplog, operation, failure
):
    calls, responses = transport
    secret = f"{URL} {TOKEN} {DIGEST}"
    responses.append(
        ReadTimeoutError(None, URL, secret)
        if failure == "timeout"
        else HTTPResponse(body=secret.encode(), status=503, headers={"Location": URL})
        if failure == "http"
        else {"code": 40001, "message": secret, "request_id": "failure"}
        if failure == "business"
        else HTTPResponse(body=secret.encode(), status=200)
        if failure == "malformed"
        else HTTPResponse(body=secret.encode(), status=302, headers={"Location": URL})
    )
    with caplog.at_level(logging.DEBUG):
        with official_client(access_token=TOKEN) as client:
            with pytest.raises(DomainError) as error:
                operation(client, budget)
    assert error.value.code == (
        "material_response_unknown"
        if failure == "malformed"
        else "tiktok_response_error"
    )
    assert len(calls) == 1
    visible = "".join(traceback.format_exception(error.value)) + caplog.text
    for value in (URL, TOKEN, DIGEST, "offline-url-secret"):
        assert value not in visible


def test_known_receipt_survives_failure_after_return(transport, budget, monkeypatch):
    transport[1].append(
        {"code": 0, "data": [{"video_id": "actual-vid", "material_id": "actual-mid"}]}
    )
    receipt = None
    with pytest.raises(RuntimeError, match="cleanup fixture"):
        with official_client(access_token=TOKEN) as client:
            receipt = parse_upload(upload(client, budget))

            def broken_clear():
                raise RuntimeError("cleanup fixture")

            monkeypatch.setattr(client.rest_client.pool_manager, "clear", broken_clear)
    assert receipt == {"video_id": "actual-vid", "mid": "actual-mid"}


@pytest.mark.parametrize("operation", [upload, read])
def test_worker_interrupt_remains_an_interrupt(transport, budget, operation):
    transport[1].append(SoftTimeLimitExceeded())
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(SoftTimeLimitExceeded):
            operation(client, budget)


@pytest.mark.parametrize("method", ["info", "search"])
def test_existing_read_wrappers_accept_actual_budget_and_reject_expired_before_io(
    transport, budget, method
):
    from datetime import timedelta

    from app.modules.materials.sdk_assets import read_video, search_videos

    wrapper = read_video if method == "info" else search_videos
    kwargs = (
        {"advertiser_id": "actual-account", "video_id": "known-vid"}
        if method == "info"
        else {"advertiser_id": "actual-account", "page": 1}
    )
    with official_client(access_token=TOKEN) as client:
        with pytest.raises(DomainError) as error:
            wrapper(
                client,
                budget=replace(
                    budget, deadline=datetime.now(UTC) - timedelta(seconds=1)
                ),
                **kwargs,
            )
        assert error.value.code == "material_deadline"
        assert transport[0] == []
        transport[1].append({"code": 0, "data": {"list": []}})
        wrapper(
            client,
            budget=replace(budget, deadline=datetime.now(UTC) + timedelta(seconds=12)),
            **kwargs,
        )
    timeout = transport[0][0][2]["timeout"]
    assert timeout.connect_timeout + timeout.read_timeout <= 7
