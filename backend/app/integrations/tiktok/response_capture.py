"""Capture existing video responses without logging requests or sending new calls."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from business_api_client.rest import ApiException  # type: ignore[import-untyped]

VIDEO_RESPONSE_OPERATIONS = frozenset(
    {"materials.upload_video_url", "materials.get_videos", "materials.search_videos"}
)


@dataclass(frozen=True)
class ProviderResponse:
    operation: str
    advertiser_id: str
    format: Literal["mcp_tool_result_json", "sdk_http_body"]
    body: bytes = field(repr=False)
    http_status: int | None = None


ResponseObserver = Callable[[ProviderResponse], None]


@contextmanager
def capture_sdk_response(
    client: Any,
    *,
    operation: str,
    advertiser_id: str | None,
    observer: ResponseObserver | None,
) -> Iterator[None]:
    if observer is None or operation not in VIDEO_RESPONSE_OPERATIONS:
        yield
        return
    assert advertiser_id is not None
    original = client.request
    missing = object()
    previous = client.__dict__.get("request", missing)

    def observe(body: bytes | str, status: int | None) -> None:
        observer(
            ProviderResponse(
                operation,
                advertiser_id,
                "sdk_http_body",
                body.encode("utf-8") if isinstance(body, str) else body,
                status,
            )
        )

    def request(*args: Any, **kwargs: Any) -> Any:
        # 在 SDK 反序列化/业务错误处理前保存正文；不采集请求头、令牌或请求 URL。
        try:
            response = original(*args, **kwargs)
        except ApiException as error:
            if isinstance(error.body, bytes | str):
                observe(error.body, error.status)
            raise
        observe(response.data, response.status)
        return response

    client.request = request
    try:
        yield
    finally:
        if previous is missing:
            del client.request
        else:
            client.request = previous
