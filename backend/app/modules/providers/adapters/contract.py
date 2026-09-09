"""Offline-reviewed transport contract. No global session or implicit retry."""

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.core.errors import DomainError

type JsonDict = dict[str, Any]


class ProviderClient(Protocol):
    def search(self, title: str, page: int) -> JsonDict: ...
    def find_existing(
        self, drama_id: str, config: JsonDict, cursor: str | None
    ) -> JsonDict: ...
    def create_step(self, step: str, payload: JsonDict) -> JsonDict: ...
    def read_link(self, remote_id: str) -> JsonDict: ...


@dataclass(frozen=True)
class ProviderSession:
    connection_id: UUID
    application_id: str
    http: httpx.Client = field(repr=False)
    client: ProviderClient = field(repr=False)


def failure(code: str, *, retryable: bool = False) -> DomainError:
    return DomainError(code, "版权方操作未完成，请根据错误状态处理", retryable)


def request_json(
    http: httpx.Client, method: str, url: str, *, write: bool = False, **kwargs: Any
) -> tuple[JsonDict, httpx.Response]:
    # httpcore DEBUG response-header traces can include login Set-Cookie.
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logging.getLogger(name).disabled = True
    try:
        response = http.request(
            method, url, timeout=30, follow_redirects=False, **kwargs
        )
        if response.status_code in {401, 403}:
            raise failure(
                "provider_session_expired"
                if response.status_code == 401
                else "provider_application_forbidden"
            )
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise failure(
                "provider_result_unknown" if write else "provider_unavailable",
                retryable=not write,
            )
        if not 200 <= response.status_code < 300:
            raise failure("provider_rejected")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError
        return body, response
    except httpx.HTTPError:
        raise failure(
            "provider_result_unknown" if write else "provider_unavailable",
            retryable=not write,
        ) from None
    except ValueError:
        raise failure(
            "provider_result_unknown" if write else "provider_schema_unsupported"
        ) from None


def positive(value: object) -> int:
    if isinstance(value, bool):
        raise failure("provider_request_invalid")
    try:
        number = int(str(value))
    except ValueError:
        raise failure("provider_request_invalid") from None
    if number < 1 or str(number) != str(value):
        raise failure("provider_request_invalid")
    return number


def external_id(value: object) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not str(value)
        or len(str(value)) > 255
    ):
        raise failure("provider_schema_unsupported")
    return str(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise failure("provider_schema_unsupported")
    return value


def counted_page(
    data: object, page: int, page_size: int = 20
) -> tuple[list[JsonDict], str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise failure("provider_schema_unsupported")
    rows, count = data["data"], data.get("count")
    if (
        type(count) is not int
        or count < 0
        or any(not isinstance(row, dict) for row in rows)
        or len(rows) > page_size
    ):
        raise failure("provider_schema_unsupported")
    offset = (page - 1) * page_size
    if count < offset + len(rows) or (
        count > offset + len(rows) and len(rows) != page_size
    ):
        raise failure("provider_schema_unsupported")
    return rows, str(page + 1) if count > page * page_size else None
