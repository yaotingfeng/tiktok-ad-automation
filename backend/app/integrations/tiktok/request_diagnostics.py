"""观察现有 SDK HTTP 边界；不新增请求、不保存请求正文或完整响应。"""

import json
from contextlib import contextmanager
from time import monotonic
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

from app.core.diagnostics import emit, error_fields, identifier, trace_context


@contextmanager
def trace_sdk_request(client: Any, **fields: Any):
    original = client.request
    missing = object()
    previous = client.__dict__.get("request", missing)
    # SDK 在线程池执行 request，显式捕获父任务的关联信息。
    identity = {**(trace_context.get() or {}), **fields}

    def request(*args: Any, **kwargs: Any):
        started = monotonic()
        emit("sdk_http_started", **identity)
        try:
            response = original(*args, **kwargs)
        except BaseException as error:
            status = getattr(error, "status", None)
            emit(
                "sdk_http_failed",
                **identity,
                **error_fields(error),
                http_status=status if type(status) is int else None,
                duration_ms=round((monotonic() - started) * 1000, 3),
            )
            raise
        metadata: dict[str, Any] = {}
        try:
            body = response.data
            if isinstance(body, bytes | str) and len(body) <= 8 * 1024 * 1024:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    metadata["request_id"] = identifier(parsed.get("request_id"))
                    if type(parsed.get("code")) is int:
                        metadata["remote_code"] = parsed["code"]
                    data = parsed.get("data")
                    if isinstance(data, list) and len(data) == 1:
                        data = data[0]
                    if isinstance(data, dict):
                        for key in (
                            "image_id",
                            "video_id",
                            "campaign_id",
                            "adgroup_id",
                            "smart_plus_ad_id",
                            "portfolio_id",
                        ):
                            if identifier(data.get(key)):
                                metadata[key] = data[key]
        except SoftTimeLimitExceeded:
            raise
        except Exception as error:
            metadata["metadata_error_type"] = type(error).__name__
        status = getattr(response, "status", None)
        emit(
            "sdk_http_response",
            **identity,
            **metadata,
            http_status=status if type(status) is int else None,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )
        return response

    client.request = request
    try:
        yield
    finally:
        if previous is missing:
            del client.request
        else:
            client.request = previous
