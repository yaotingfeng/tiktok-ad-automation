"""Official target-account video cover -> image library contract.

Signed preview URLs are ephemeral inputs, never persisted as image identities.
The generated URL upload method hardcodes multipart; this documented JSON mode
therefore uses the same pinned official ApiClient generic entrypoint.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from business_api_client.api.file_api import FileApi  # type: ignore[import-untyped]

from app.core.credentials import decrypt_credentials
from app.core.errors import DomainError
from app.integrations.tiktok.sdk import checked_data
from app.modules.accounts.models import TikTokConnection
from app.modules.materials.sdk_assets import INFO_ENDPOINT as VIDEO_INFO_ENDPOINT
from app.modules.materials.sdk_assets import read_video, verified_video

UPLOAD_ENDPOINT = "/open_api/v1.3/file/image/ad/upload/"
INFO_ENDPOINT = "/open_api/v1.3/file/image/ad/info/"
SEARCH_ENDPOINT = "/open_api/v1.3/file/image/ad/search/"
SUGGEST_ENDPOINT = "/open_api/v1.3/file/video/suggestcover/"
PAGE_SIZE = 100
MAX_IMAGES = 10000
_SCOPE_PARENTS = {
    VIDEO_INFO_ENDPOINT: {6, 61, 610},
    SUGGEST_ENDPOINT: {6, 61, 612},
    UPLOAD_ENDPOINT: {6, 60, 601},
    INFO_ENDPOINT: {6, 60, 600},
    SEARCH_ENDPOINT: {6, 60, 600},
}


@dataclass(frozen=True)
class VideoCover:
    url: str | None = field(repr=False)
    width: int
    height: int


@dataclass(frozen=True)
class ImageReceipt:
    image_id: str
    signature: str | None


def _error() -> DomainError:
    return DomainError("cover_schema_unsupported", "目标素材封面信息无法核实")


def _identifier(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= 255
        else None
    )


def _dimension(value: object) -> bool:
    return type(value) is int and 0 < value <= 65536


def _signature(value: object) -> str | None:
    return (
        value.lower()
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value)
        else None
    )


def _url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16384
        or any(c.isspace() for c in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            return None
    except ValueError:
        return None
    # The app never downloads this address; it passes the URL obtained from the
    # official video endpoint back to TikTok's image ingestion service.
    return value


def require_cover_scopes(connection: TikTokConnection, *, endpoint: str) -> None:
    """Separate image/video OAuth leaves; a video-upload grant is insufficient."""
    try:
        if connection.status != "ACTIVE" or not connection.credential_ciphertext:
            raise ValueError
        private = decrypt_credentials(
            tenant_id=connection.tenant_id, ciphertext=connection.credential_ciphertext
        )
        scopes = json.loads(private.get("scope", "null"))
        if not isinstance(scopes, list) or any(
            type(scope) is not int for scope in scopes
        ):
            raise ValueError
        if endpoint not in _SCOPE_PARENTS or not set(scopes) & _SCOPE_PARENTS[endpoint]:
            raise ValueError
    except ValueError, TypeError, DomainError:
        raise DomainError(
            "cover_permission_unverified", "当前连接尚无已核实的封面读写权限"
        ) from None


def _call(
    client: Any,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    method = "POST" if body is not None else "GET"
    headers = {
        "Access-Token": client.default_headers.get("Access-Token", ""),
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    future = client.call_api(
        path,
        method,
        {},
        list((query or {}).items()),
        headers,
        body=body,
        response_type="InlineResponse200",
        auth_settings=[],
        _return_http_data_only=True,
        async_req=True,
        _request_timeout=(5, 10 if body is not None else 30),
    )
    return checked_data(future.get())


def read_video_cover(
    client: Any, *, advertiser_id: str, video_id: str, md5: str
) -> VideoCover:
    data = read_video(client, advertiser_id=advertiser_id, video_id=video_id)
    evidence = verified_video(data, md5=md5)
    if evidence is None or evidence["video_id"] != video_id:
        raise _error()
    row = data["list"][0]
    if not _dimension(row.get("width")) or not _dimension(row.get("height")):
        raise _error()
    return VideoCover(_url(row.get("video_cover_url")), row["width"], row["height"])


def suggest_cover(
    client: Any, *, advertiser_id: str, video_id: str, width: int, height: int
) -> str | None:
    data = _call(
        client,
        SUGGEST_ENDPOINT,
        query={
            "advertiser_id": advertiser_id,
            "video_id": video_id,
            "poster_number": 10,
        },
    )
    rows = data.get("list")
    if (
        not isinstance(rows, list)
        or len(rows) > 10
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise _error()
    for row in rows:
        if (
            _dimension(row.get("width"))
            and _dimension(row.get("height"))
            and row["width"] * height == row["height"] * width
        ):
            if url := _url(row.get("url")):
                return url
    return None


def upload_cover(
    client: Any, *, advertiser_id: str, url: str, remote_name: str
) -> ImageReceipt:
    if (
        not _url(url)
        or not isinstance(remote_name, str)
        or not 1 <= len(remote_name) <= 100
        or any(ord(c) < 32 for c in remote_name)
    ):
        raise DomainError("cover_request_invalid", "封面上传参数无效")
    data = _call(
        client,
        UPLOAD_ENDPOINT,
        body={
            "advertiser_id": advertiser_id,
            "upload_type": "UPLOAD_BY_URL",
            "image_url": url,
            "file_name": remote_name,
        },
    )
    identity = _identifier(data.get("image_id"))
    if identity is None:
        raise _error()
    # Optional malformed or missing metadata must not erase a received image ID.
    return ImageReceipt(identity, _signature(data.get("signature")))


def read_image(client: Any, *, advertiser_id: str, image_id: str) -> dict[str, Any]:
    return checked_data(
        FileApi(client).file_image_ad_info(
            advertiser_id=advertiser_id,
            image_ids=[image_id],
            access_token=client.default_headers.get("Access-Token", ""),
            _request_timeout=(5, 30),
        )
    )


def search_images(client: Any, *, advertiser_id: str, page: int) -> dict[str, Any]:
    if type(page) is not int or not 1 <= page <= MAX_IMAGES // PAGE_SIZE:
        raise DomainError("cover_search_incomplete", "封面核查超出可核实的分页范围")
    return _call(
        client,
        SEARCH_ENDPOINT,
        query={"advertiser_id": advertiser_id, "page": page, "page_size": PAGE_SIZE},
    )


def verified_image(
    data: dict[str, Any],
    *,
    image_id: str,
    remote_name: str,
    signature: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, str] | None:
    rows = data.get("list")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    actual_signature = _signature(row.get("signature"))
    if (
        row.get("image_id") != image_id
        or row.get("file_name") != remote_name
        or row.get("displayable") is not True
        or not actual_signature
        or (signature is not None and actual_signature != signature.lower())
        or not _dimension(row.get("width"))
        or not _dimension(row.get("height"))
    ):
        return None
    if width is not None or height is not None:
        if not _dimension(width) or not _dimension(height):
            return None
        if row["width"] * height != row["height"] * width:
            return None
    return {"image_id": image_id, "signature": actual_signature}


def image_search_page(
    data: dict[str, Any], *, page: int
) -> tuple[list[dict[str, Any]], bool, int]:
    rows, info = data.get("list"), data.get("page_info")
    if (
        not isinstance(rows, list)
        or not isinstance(info, dict)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise _error()
    total, pages = info.get("total_number"), info.get("total_page")
    if (
        type(total) is not int
        or not 0 <= total <= MAX_IMAGES
        or type(pages) is not int
        or pages
        not in ({0, 1} if total == 0 else {(total + PAGE_SIZE - 1) // PAGE_SIZE})
        or type(info.get("page")) is not int
        or info["page"] != page
        or type(info.get("page_size")) is not int
        or info["page_size"] != PAGE_SIZE
        or page < 1
        or page > max(1, pages)
        or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    ):
        raise _error()
    identities = [_identifier(row.get("image_id")) for row in rows]
    if None in identities or len(set(identities)) != len(identities):
        raise _error()
    allowed = {"image_id", "file_name", "displayable", "signature", "width", "height"}
    return (
        [{key: value for key, value in row.items() if key in allowed} for row in rows],
        page >= pages,
        total,
    )
