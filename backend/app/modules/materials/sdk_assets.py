"""Pinned official asset methods. Callers admit each request and bound its process.

Never persist these raw responses: use the allowlisted evidence helpers below.
Upload success proves receipt only; a separate account-scoped read verifies use.
"""

import ipaddress
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import business_api_client.tiktok_business.tiktok_exceptions as sdk_errors  # type: ignore[import-untyped]
from business_api_client.api.file_api import FileApi  # type: ignore[import-untyped]
from business_api_client.models.filtering_video_ad_search import (  # type: ignore[import-untyped]
    FilteringVideoAdSearch,
)
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from urllib3.exceptions import HTTPError

from app.core.errors import DomainError
from app.integrations.tiktok.accounts import paged_rows
from app.integrations.tiktok.sdk import (
    SDK_SCOPE_INTERRUPTS,
    checked_data,
)
from app.integrations.tiktok.sdk import (
    AccountAdmissionDeferred as SdkAdmissionDeferred,
)
from app.integrations.tiktok.sdk import (
    admitted_account_call as admitted_asset_call,
)

# Re-export the real Redis-backed request scope, shared by upload and distribution.
__all__ = [
    "RemoteCallBudget",
    "SdkAdmissionDeferred",
    "SourcePreview",
    "admitted_asset_call",
    "read_source_preview",
    "upload_video_url",
]
UPLOAD_ENDPOINT = "/open_api/v1.3/file/video/ad/upload/"
INFO_ENDPOINT = "/open_api/v1.3/file/video/ad/info/"
SEARCH_ENDPOINT = "/open_api/v1.3/file/video/ad/search/"
SHARE_ENDPOINT = "/open_api/v1.3/creative/asset/share/"
PAGE_SIZE = 100
# Conservative formats already recognized by the application's upload naming
# contract; this is not a claim about every format accepted by TikTok.
_REMOTE_VIDEO_FORMATS = frozenset({"mp4", "mov", "m4v", "avi", "webm", "mpeg", "3gp"})


@dataclass(frozen=True)
class RemoteCallBudget:
    """Actual worker deadline and admitted endpoint policy, supplied by caller.

    Socket timeouts cannot enforce a whole-task deadline. The caller must run
    in a process with this hard limit and acquire the corresponding Redis lease.
    """

    deadline: datetime
    hard_limit_seconds: int
    lease_ms: int

    def timeout(self, *, upload: bool) -> tuple[float, float]:
        if (
            type(self.hard_limit_seconds) is not int
            or self.hard_limit_seconds <= 5
            or type(self.lease_ms) is not int
            or self.lease_ms <= (self.hard_limit_seconds + 5) * 1000
            or not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise DomainError("admission_policy_invalid", "素材调用执行期限或租约无效")
        available = (
            min(
                (self.deadline - datetime.now(UTC)).total_seconds(),
                self.hard_limit_seconds,
            )
            - 5
        )
        if available <= 0:
            raise DomainError("material_deadline", "素材处理已到达本次期限")
        connect = min(10 if upload else 5, available / 2)
        return connect, min(300 if upload else 30, available - connect)


@dataclass(frozen=True)
class SourcePreview:
    """Fresh account-scoped evidence; URL stays only in the current call's memory."""

    advertiser_id: str
    video_id: str
    mid: str | None
    md5: str = field(repr=False)
    url: str = field(repr=False)
    width: int
    height: int
    size: int
    duration: float
    format: str
    displayable: bool = True


def _remote_request_error() -> DomainError:
    return DomainError("material_request_invalid", "素材远程请求参数无效")


def _remote_identifier(value: object, *, limit: int = 128) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= limit
        and value == value.strip()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _trusted_md5(value: object) -> str:
    # Syntax is necessary, not proof of origin. Caller supplies the durable
    # whole-file verifier result, never an ETag or a browser-declared digest.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise DomainError("material_digest_missing", "素材缺少可核实内容摘要")
    return value.lower()


def _https_host(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16384
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
        or "#" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.netloc.endswith(":")
            or parsed.fragment
            or not _dns_host(host)
        ):
            return None
    except ValueError:
        return None
    return host


def _dns_host(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 253
        or value != value.lower()
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return True
    return False


def _positive_duration(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def upload_video_url(
    client: Any,
    *,
    advertiser_id: str,
    video_url: str,
    remote_name: str,
    md5: str,
    budget: RemoteCallBudget,
) -> object:
    """One official URL POST; no download, file body, retry or receipt rewriting.

    Only pass a just-issued, authorized object/verified source URL. Parse and
    save known receipt IDs before leaving caller-owned SDK/admission scopes.
    Any post-send failure remains unknown until original-path read-back.
    """
    digest = _trusted_md5(md5)
    if (
        not _remote_identifier(advertiser_id)
        or not _remote_identifier(remote_name, limit=100)
        or _https_host(video_url) is None
    ):
        raise _remote_request_error()
    timeout = budget.timeout(upload=True)
    try:
        return FileApi(client).ad_video_upload(
            access_token=client.default_headers["Access-Token"],
            advertiser_id=advertiser_id,
            upload_type="UPLOAD_BY_URL",
            video_url=video_url,
            file_name=remote_name,
            video_signature=digest,
            auto_bind_enabled=False,
            auto_fix_enabled=False,
            _request_timeout=timeout,
        )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except ApiException, sdk_errors.TiktokSDKError, HTTPError:
        # Same classification as sdk_client; never expose provider body,
        # Location headers, signed URLs, signatures or tokens via tracebacks.
        raise DomainError("tiktok_response_error", "TikTok 请求未成功") from None
    except Exception:
        raise DomainError("material_response_unknown", "素材请求结果待核实") from None


def read_source_preview(
    client: Any,
    *,
    advertiser_id: str,
    video_id: str,
    md5: str,
    allowed_hosts: frozenset[str],
    budget: RemoteCallBudget,
) -> SourcePreview:
    """Read exactly one current authorized source VID, never a historical URL.

    Caller revalidates tenant/BC/actor/connection/account authorization and
    supplies deployment-vetted exact CDN hosts. No permissive CDN defaults,
    suffix matching, redirect following, or URL persistence occurs here.
    """
    digest = _trusted_md5(md5)
    if not _remote_identifier(advertiser_id) or not _remote_identifier(video_id):
        raise _remote_request_error()
    if (
        not isinstance(allowed_hosts, frozenset)
        or not allowed_hosts
        or any(not _dns_host(host) for host in allowed_hosts)
    ):
        raise DomainError("material_preview_unverified", "尚无已核实的素材预览域名策略")
    timeout = budget.timeout(upload=False)
    try:
        data = checked_data(
            FileApi(client).ad_video_info(
                advertiser_id=advertiser_id,
                video_ids=[video_id],
                access_token=client.default_headers["Access-Token"],
                _request_timeout=timeout,
            )
        )
    except SDK_SCOPE_INTERRUPTS:
        raise
    except ApiException, sdk_errors.TiktokSDKError, HTTPError:
        raise DomainError("tiktok_response_error", "TikTok 请求未成功") from None
    except DomainError:
        raise
    except Exception:
        raise DomainError("material_response_unknown", "素材请求结果待核实") from None
    rows = video_rows(data)
    if len(rows) != 1:
        raise _schema_error()
    row = rows[0]
    if (
        row.get("video_id") != video_id
        or row.get("displayable") is not True
        or not isinstance(row.get("signature"), str)
        or row["signature"].lower() != digest
        or ("advertiser_id" in row and row["advertiser_id"] != advertiser_id)
        or ("material_id" in row and not _remote_identifier(row["material_id"]))
        or any(
            type(row.get(key)) is not int or not 0 < row[key] <= 65536
            for key in ("width", "height")
        )
        or type(row.get("size")) is not int
        or row["size"] <= 0
        or not _positive_duration(row.get("duration"))
        or not isinstance(row.get("format"), str)
        or row["format"].lower() not in _REMOTE_VIDEO_FORMATS
    ):
        raise _schema_error()
    if _https_host(row.get("preview_url")) not in allowed_hosts:
        raise DomainError("material_preview_unverified", "素材预览地址缺失或未经核实")
    return SourcePreview(
        advertiser_id=advertiser_id,
        video_id=video_id,
        mid=row.get("material_id"),
        md5=digest,
        url=row["preview_url"],
        width=row["width"],
        height=row["height"],
        size=row["size"],
        duration=float(row["duration"]),
        format=row["format"],
    )


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


def read_video(
    client: Any,
    *,
    advertiser_id: str,
    video_id: str,
    budget: RemoteCallBudget | None = None,
) -> dict[str, Any]:
    return checked_data(
        FileApi(client).ad_video_info(
            advertiser_id=advertiser_id,
            video_ids=[video_id],
            access_token=client.default_headers["Access-Token"],
            _request_timeout=budget.timeout(upload=False) if budget else (5, 30),
        )
    )


def search_videos(
    client: Any,
    *,
    advertiser_id: str,
    page: int,
    material_ids: list[str] | None = None,
    budget: RemoteCallBudget | None = None,
) -> dict[str, Any]:
    kwargs = {}
    if material_ids:
        kwargs["filtering"] = FilteringVideoAdSearch(material_ids=material_ids)
    return checked_data(
        FileApi(client).ad_video_search(
            advertiser_id=advertiser_id,
            access_token=client.default_headers["Access-Token"],
            page=page,
            page_size=PAGE_SIZE,
            _request_timeout=budget.timeout(upload=False) if budget else (5, 30),
            **kwargs,
        )
    )


def _schema_error() -> DomainError:
    return DomainError("unsupported_material_schema", "平台素材返回结构尚不支持核实")


def _id(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return (
        value
        if isinstance(value, str) and value.strip() and len(value) <= 128
        else None
    )


def identity(row: dict[str, Any]) -> dict[str, str]:
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


def video_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _schema_error()
    return rows


def verified_video(
    data: dict[str, Any],
    *,
    md5: str,
    expected_video_id: str | None = None,
    expected_size: int | None = None,
) -> dict[str, str] | None:
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
    if expected_video_id is not None and row.get("video_id") != expected_video_id:
        return None
    if expected_size is not None and (
        type(row.get("size")) is not int
        or row["size"] != expected_size
        or type(row.get("width")) is not int
        or row["width"] <= 0
        or type(row.get("height")) is not int
        or row["height"] <= 0
        or type(row.get("duration")) not in (int, float)
        or not math.isfinite(row["duration"])
        or row["duration"] <= 0
        or not isinstance(row.get("format"), str)
        or row["format"].lower() not in _REMOTE_VIDEO_FORMATS
    ):
        return None
    return identity(row)


def search_page(
    data: dict[str, Any], *, page: int, remote_name: str, md5: str
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


def share_video(
    client: Any,
    *,
    source_advertiser_id: str,
    source_mid: str,
    target_advertiser_id: str,
) -> dict[str, Any]:
    """Generated method only; workflow must first establish capability evidence.

    No production source/target pair has verified share permission/mapping
    semantics in this deployment yet. Readiness consequently never chooses it.
    """
    from business_api_client.api.creative_management_api import (  # type: ignore[import-untyped]
        CreativeManagementApi,
    )
    from business_api_client.models.asset_share_body import (  # type: ignore[import-untyped]
        AssetShareBody,
    )

    body = AssetShareBody(
        advertiser_id=source_advertiser_id,
        asset_type="VIDEO",
        material_ids=[source_mid],
        shared_advertiser_ids=[target_advertiser_id],
    )
    return checked_data(
        CreativeManagementApi(client).creative_asset_share(
            access_token=client.default_headers["Access-Token"],
            body=body,
            _request_timeout=(5, 30),
        )
    )
