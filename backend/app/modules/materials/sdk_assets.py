"""Pinned official asset methods. Callers admit each request and bound its process.

Never persist these raw responses: use the allowlisted evidence helpers below.
Upload success proves receipt only; a separate account-scoped read verifies use.
"""

from typing import Any

from business_api_client.api.file_api import FileApi
from business_api_client.models.filtering_video_ad_search import FilteringVideoAdSearch

from app.core.errors import DomainError
from app.integrations.tiktok.accounts import paged_rows
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred as SdkAdmissionDeferred,
)
from app.integrations.tiktok.sdk import (
    admitted_account_call as admitted_asset_call,
)
from app.integrations.tiktok.sdk import (
    checked_data,
)

# Re-export the real Redis-backed request scope, shared by upload and distribution.
__all__ = ["SdkAdmissionDeferred", "admitted_asset_call"]
UPLOAD_ENDPOINT = "/open_api/v1.3/file/video/ad/upload/"
INFO_ENDPOINT = "/open_api/v1.3/file/video/ad/info/"
SEARCH_ENDPOINT = "/open_api/v1.3/file/video/ad/search/"
SHARE_ENDPOINT = "/open_api/v1.3/creative/asset/share/"
PAGE_SIZE = 100


def upload_video(
    client: Any, *, advertiser_id: str, local_path: str, remote_name: str, md5: str
) -> object:
    return FileApi(client).ad_video_upload(
        access_token=client.default_headers["Access-Token"],
        advertiser_id=advertiser_id,
        upload_type="UPLOAD_BY_FILE",
        video_file=local_path,
        file_name=remote_name,
        video_signature=md5,
        auto_bind_enabled=False,
        auto_fix_enabled=False,
        _request_timeout=(10, 300),
    )


def read_video(client: Any, *, advertiser_id: str, video_id: str) -> dict:
    return checked_data(
        FileApi(client).ad_video_info(
            advertiser_id=advertiser_id,
            video_ids=[video_id],
            access_token=client.default_headers["Access-Token"],
            _request_timeout=(5, 30),
        )
    )


def search_videos(
    client: Any, *, advertiser_id: str, page: int, material_ids: list[str] | None = None
) -> dict:
    kwargs = {}
    if material_ids:
        kwargs["filtering"] = FilteringVideoAdSearch(material_ids=material_ids)
    return checked_data(
        FileApi(client).ad_video_search(
            advertiser_id=advertiser_id,
            access_token=client.default_headers["Access-Token"],
            page=page,
            page_size=PAGE_SIZE,
            _request_timeout=(5, 30),
            **kwargs,
        )
    )


def _schema_error() -> DomainError:
    return DomainError("unsupported_material_schema", "平台素材返回结构尚不支持核实")


def _id(row: dict, key: str) -> str | None:
    value = row.get(key)
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= 128
        else None
    )


def identity(row: dict) -> dict[str, str]:
    video_id = _id(row, "video_id")
    if not video_id:
        raise _schema_error()
    result = {"video_id": video_id}
    # MID is material_id, never a fallback VID.
    if mid := _id(row, "material_id"):
        result["mid"] = mid
    return result


def parse_upload(response: object) -> dict[str, str]:
    # Pinned ApiClient has already checked code; upload data is an array while
    # checked_data deliberately supports dictionary data for read APIs only.
    if isinstance(response, dict):
        if set(response) != {"data", "request_id"}:
            raise _schema_error()
        data = response["data"]
    else:
        convert = getattr(response, "to_dict", None)
        raw = convert() if callable(convert) else None
        if (
            not isinstance(raw, dict)
            or type(raw.get("code")) is not int
            or raw["code"] != 0
        ):
            raise _schema_error()
        data = raw.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise _schema_error()
    return identity(data[0])


def video_rows(data: dict) -> list[dict]:
    rows = data.get("list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _schema_error()
    return rows


def verified_video(data: dict, *, md5: str) -> dict[str, str] | None:
    rows = video_rows(data)
    # The one-ID lookup must have exactly one strong content match. Unknown
    # statuses, multiple records and missing signatures never imply readiness.
    if len(rows) != 1:
        return None
    row = rows[0]
    if (
        row.get("displayable") is not True
        or row.get("signature") != md5
        or not _id(row, "video_id")
    ):
        return None
    return identity(row)


def search_page(
    data: dict, *, page: int, remote_name: str, md5: str
) -> tuple[list[dict[str, str]], bool]:
    rows, last = paged_rows(data, page=page, page_size=PAGE_SIZE)
    matches = [
        identity(row)
        for row in rows
        if row.get("file_name") == remote_name
        and row.get("signature") == md5
        and _id(row, "video_id")
    ]
    return matches, last
