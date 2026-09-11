"""Pinned official asset methods. Callers admit each request and bound its process.

Never persist these raw responses: use the allowlisted evidence helpers below.
Upload success proves receipt only; a separate account-scoped read verifies use.
"""

import ipaddress
import math
import re
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import DomainError
from app.integrations.tiktok.accounts import paged_rows
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
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
__all__ = [
    "SdkAdmissionDeferred",
    "admitted_asset_call",
]
UPLOAD_ENDPOINT = "/open_api/v1.3/file/video/ad/upload/"
INFO_ENDPOINT = "/open_api/v1.3/file/video/ad/info/"
SEARCH_ENDPOINT = "/open_api/v1.3/file/video/ad/search/"
PAGE_SIZE = 100
# Conservative formats already recognized by the application's upload naming
# contract; this is not a claim about every format accepted by TikTok.
_REMOTE_VIDEO_FORMATS = frozenset({"mp4", "mov", "m4v", "avi", "webm", "mpeg", "3gp"})


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


def source_preview(
    data: dict[str, Any],
    *,
    advertiser_id: str,
    video_id: str,
    allowed_hosts: frozenset[str],
    expected_md5: str | None = None,
    evidence: CallEvidence = CallEvidence(),
) -> material_types.SourcePreview:
    if not allowed_hosts or any(not _dns_host(host) for host in allowed_hosts):
        raise DomainError("material_preview_unverified", "尚无已核实的素材预览域名策略")
    rows = video_rows(data)
    if len(rows) != 1:
        raise _schema_error()
    row = rows[0]
    if (
        row.get("video_id") != video_id
        or row.get("displayable") is not True
        or not isinstance(row.get("signature"), str)
        or not re.fullmatch(r"[0-9a-fA-F]{32}", row["signature"])
        or (
            expected_md5 is not None
            and row["signature"].lower() != expected_md5.lower()
        )
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
    return material_types.SourcePreview(
        advertiser_id=advertiser_id,
        video_id=video_id,
        mid=row.get("material_id"),
        md5=row["signature"].lower(),
        url=row["preview_url"],
        width=row["width"],
        height=row["height"],
        size=row["size"],
        duration=float(row["duration"]),
        format=row["format"],
        evidence=evidence,
    )


def _response(response: object) -> McpBusinessResponse:
    request_id = (
        response.get("request_id")
        if isinstance(response, dict)
        else getattr(response, "request_id", None)
    )
    if type(request_id) is not str or not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,128}", request_id
    ):
        request_id = None
    return McpBusinessResponse(
        checked_data(response), CallEvidence(request_id=request_id)
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


def video_identity(
    record: material_types.VideoRecord | None,
    *,
    advertiser_id: str,
    video_id: str,
    md5: str,
    expected_size: int,
) -> dict[str, str] | None:
    """发布只采用同账户详情的实际摘要，不用请求摘要填补缺失的远端事实。"""
    if record is None or (record.advertiser_id, record.video_id) != (
        advertiser_id,
        video_id,
    ):
        return None
    from dataclasses import asdict

    row = asdict(record)
    row["signature"], row["material_id"] = row.pop("md5"), row.pop("mid")
    return verified_video(
        {"list": [row]},
        md5=md5,
        expected_video_id=video_id,
        expected_size=expected_size,
    )


def _record_fields(
    row: dict[str, Any], *, advertiser_id: str, identity_key: str
) -> None:
    if not _remote_identifier(row.get(identity_key)) or (
        "advertiser_id" in row and row["advertiser_id"] != advertiser_id
    ):
        raise _schema_error()
    for name in ("width", "height", "size"):
        if row.get(name) is not None and (type(row[name]) is not int or row[name] <= 0):
            raise _schema_error()
    if row.get("duration") is not None and not _positive_duration(row["duration"]):
        raise _schema_error()
    for name in ("file_name", "format"):
        if row.get(name) is not None and type(row[name]) is not str:
            raise _schema_error()
    if row.get("displayable") is not None and type(row["displayable"]) is not bool:
        raise _schema_error()
    if row.get("signature") is not None and (
        type(row["signature"]) is not str
        or not re.fullmatch(r"[0-9a-fA-F]{32}", row["signature"])
    ):
        raise _schema_error()
    if row.get("material_id") is not None and not _remote_identifier(
        row["material_id"]
    ):
        raise _schema_error()


def video_record(
    row: dict[str, Any], *, advertiser_id: str, evidence: CallEvidence
) -> material_types.VideoRecord:
    _record_fields(row, advertiser_id=advertiser_id, identity_key="video_id")
    return material_types.VideoRecord(
        advertiser_id,
        row["video_id"],
        row.get("material_id"),
        row["signature"].lower() if row.get("signature") is not None else None,
        row.get("file_name"),
        row.get("width"),
        row.get("height"),
        row.get("size"),
        float(row["duration"]) if row.get("duration") is not None else None,
        row.get("format"),
        row.get("displayable"),
        evidence,
    )


def image_record(
    row: dict[str, Any], *, advertiser_id: str, evidence: CallEvidence
) -> material_types.ImageRecord:
    _record_fields(row, advertiser_id=advertiser_id, identity_key="image_id")
    return material_types.ImageRecord(
        advertiser_id,
        row["image_id"],
        row["signature"].lower() if row.get("signature") is not None else None,
        row.get("file_name"),
        row.get("width"),
        row.get("height"),
        row.get("displayable"),
        evidence,
    )


class MaterialReadAdapter:
    """通道共用纯业务解析；_call 由实际 SDK/MCP 适配器提供，不持有数据库或令牌。"""

    def __init__(self, *, preview_allowed_hosts: frozenset[str]):
        self._preview_allowed_hosts = preview_allowed_hosts
        self._searches: dict[
            tuple[str, str, tuple[str, ...]], tuple[int, set[str], int, int | None]
        ] = {}

    def _call(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: material_types.RemoteCallBudget,
    ) -> McpBusinessResponse:
        raise NotImplementedError

    def _read(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: material_types.RemoteCallBudget,
    ) -> McpBusinessResponse:
        if not _remote_identifier(advertiser_id):
            raise _remote_request_error()
        budget.timeout(upload=False)
        response = self._call(
            operation,
            advertiser_id,
            {"advertiser_id": advertiser_id, **arguments},
            budget,
        )
        if type(response.data) is not dict:
            raise _schema_error()
        return response

    def _video_response(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        budget: material_types.RemoteCallBudget,
    ) -> McpBusinessResponse:
        if not _remote_identifier(video_id):
            raise _remote_request_error()
        return self._read(
            "materials.get_videos", advertiser_id, {"video_ids": [video_id]}, budget
        )

    def read_video(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        budget: material_types.RemoteCallBudget,
    ) -> material_types.VideoRecord | None:
        response = self._video_response(
            advertiser_id=advertiser_id, video_id=video_id, budget=budget
        )
        assert isinstance(response.data, dict)
        rows = video_rows(response.data)
        if not rows:
            return None
        if len(rows) != 1 or rows[0].get("video_id") != video_id:
            raise _schema_error()
        return video_record(
            rows[0], advertiser_id=advertiser_id, evidence=response.evidence
        )

    def read_source_preview(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        budget: material_types.RemoteCallBudget,
    ) -> material_types.SourcePreview:
        if (
            not isinstance(self._preview_allowed_hosts, frozenset)
            or not self._preview_allowed_hosts
            or any(not _dns_host(host) for host in self._preview_allowed_hosts)
        ):
            raise DomainError(
                "material_preview_unverified", "尚无已核实的素材预览域名策略"
            )
        response = self._video_response(
            advertiser_id=advertiser_id, video_id=video_id, budget=budget
        )
        assert isinstance(response.data, dict)
        return source_preview(
            response.data,
            advertiser_id=advertiser_id,
            video_id=video_id,
            allowed_hosts=self._preview_allowed_hosts,
            evidence=response.evidence,
        )

    def _page(
        self,
        response: McpBusinessResponse,
        *,
        advertiser_id: str,
        page: int,
        images: bool,
        material_ids: tuple[str, ...] = (),
    ) -> material_types.MaterialPage[Any]:
        from app.modules.materials.cover_sdk import image_search_page

        assert isinstance(response.data, dict)
        rows: tuple[material_types.VideoRecord | material_types.ImageRecord, ...]
        if images:
            # 旧列表投影会丢弃未知字段；先核对任何显式账户归属，再做允许字段投影。
            for item in video_rows(response.data):
                _record_fields(
                    item, advertiser_id=advertiser_id, identity_key="image_id"
                )
            raw, _, total = image_search_page(response.data, page=page)
            rows = tuple(
                image_record(
                    row, advertiser_id=advertiser_id, evidence=response.evidence
                )
                for row in raw
            )
        else:
            try:
                raw, _ = paged_rows(response.data, page=page, page_size=PAGE_SIZE)
            except DomainError:
                raise _schema_error() from None
            total = response.data["page_info"].get("total_number")
            rows = tuple(
                video_record(
                    row, advertiser_id=advertiser_id, evidence=response.evidence
                )
                for row in raw
            )
        try:
            result = material_types.MaterialPage(
                rows,
                page,
                PAGE_SIZE,
                response.data["page_info"]["total_page"],
                total,
                response.evidence,
            )
        except TypeError, ValueError, KeyError:
            raise _schema_error() from None
        ids = {
            row.image_id
            if isinstance(row, material_types.ImageRecord)
            else row.video_id
            for row in rows
        }
        if len(ids) != len(rows):
            raise _schema_error()
        key = ("images" if images else "videos", advertiser_id, material_ids)
        previous = self._searches.get(key)
        if page != 1 and previous is not None:
            previous_page, seen, pages, count = previous
            if (
                page != previous_page + 1
                or seen & ids
                or (pages, count) != (result.total_pages, result.total_number)
            ):
                raise _schema_error()
            ids = seen | ids
        self._searches[key] = (page, ids, result.total_pages, result.total_number)
        return result

    def search_videos(
        self,
        *,
        advertiser_id: str,
        page: int,
        material_ids: tuple[str, ...],
        budget: material_types.RemoteCallBudget,
    ) -> material_types.MaterialPage[material_types.VideoRecord]:
        if (
            type(page) is not int
            or not 1 <= page <= 1000
            or type(material_ids) is not tuple
            or len(material_ids) > 100
            or any(not _remote_identifier(value) for value in material_ids)
        ):
            raise _remote_request_error()
        args: dict[str, Any] = {"page": page, "page_size": PAGE_SIZE}
        if material_ids:
            args["filtering"] = {"material_ids": list(material_ids)}
        response = self._read("materials.search_videos", advertiser_id, args, budget)
        return self._page(
            response,
            advertiser_id=advertiser_id,
            page=page,
            images=False,
            material_ids=material_ids,
        )

    def read_video_cover(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        md5: str,
        budget: material_types.RemoteCallBudget,
    ) -> material_types.VideoCover:
        from app.modules.materials.cover_sdk import _dimension, _error, _url

        response = self._video_response(
            advertiser_id=advertiser_id, video_id=video_id, budget=budget
        )
        assert isinstance(response.data, dict)
        rows = video_rows(response.data)
        if len(rows) != 1:
            raise _error()
        row = rows[0]
        _record_fields(row, advertiser_id=advertiser_id, identity_key="video_id")
        normalized = {
            **row,
            "signature": row["signature"].lower()
            if row.get("signature") is not None
            else None,
        }
        record = verified_video(
            {"list": [normalized]}, md5=_trusted_md5(md5), expected_video_id=video_id
        )
        if record is None:
            raise _error()
        if not _dimension(row.get("width")) or not _dimension(row.get("height")):
            raise _error()
        return material_types.VideoCover(
            _url(row.get("video_cover_url")),
            row["width"],
            row["height"],
            response.evidence,
        )

    def suggest_cover(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        width: int,
        height: int,
        budget: material_types.RemoteCallBudget,
    ) -> material_types.VideoCover | None:
        from app.modules.materials.cover_sdk import _dimension, _error, _url

        if (
            not _remote_identifier(video_id)
            or not _dimension(width)
            or not _dimension(height)
        ):
            raise _remote_request_error()
        response = self._read(
            "materials.get_suggested_covers",
            advertiser_id,
            {"video_id": video_id, "poster_number": 10},
            budget,
        )
        assert isinstance(response.data, dict)
        rows = response.data.get("list")
        if (
            type(rows) is not list
            or len(rows) > 10
            or any(type(row) is not dict for row in rows)
        ):
            raise _error()
        for row in rows:
            if (
                _dimension(row.get("width"))
                and _dimension(row.get("height"))
                and row["width"] * height == row["height"] * width
            ):
                if url := _url(row.get("url")):
                    return material_types.VideoCover(
                        url, row["width"], row["height"], response.evidence
                    )
        return None

    def read_image(
        self,
        *,
        advertiser_id: str,
        image_id: str,
        budget: material_types.RemoteCallBudget,
    ) -> material_types.ImageRecord | None:
        if not _remote_identifier(image_id):
            raise _remote_request_error()
        response = self._read(
            "materials.get_images", advertiser_id, {"image_ids": [image_id]}, budget
        )
        assert isinstance(response.data, dict)
        rows = video_rows(response.data)
        if not rows:
            return None
        if len(rows) != 1 or rows[0].get("image_id") != image_id:
            raise _schema_error()
        return image_record(
            rows[0], advertiser_id=advertiser_id, evidence=response.evidence
        )

    def search_images(
        self, *, advertiser_id: str, page: int, budget: material_types.RemoteCallBudget
    ) -> material_types.MaterialPage[material_types.ImageRecord]:
        if type(page) is not int or not 1 <= page <= 100:
            raise _remote_request_error()
        response = self._read(
            "materials.search_images",
            advertiser_id,
            {"page": page, "page_size": PAGE_SIZE},
            budget,
        )
        return self._page(response, advertiser_id=advertiser_id, page=page, images=True)


def validate_video_upload(
    request: material_types.URLVideoUpload | material_types.FileVideoUpload,
) -> str:
    """摘要必须来自持久原件核实；这里只检查格式，不把请求摘要当远端证据。"""
    digest = _trusted_md5(request.expected_md5)
    if not _remote_identifier(request.advertiser_id) or not _remote_identifier(
        request.file_name, limit=100
    ):
        raise _remote_request_error()
    if (
        isinstance(request, material_types.URLVideoUpload)
        and _https_host(request.url) is None
    ):
        raise _remote_request_error()
    return digest


def video_upload_receipt(
    response: McpBusinessResponse, *, advertiser_id: str, channel: str
) -> material_types.VideoReceipt:
    """API数组与MCP合同对象分别解析；不搜索嵌套ID，不按成功文案补回执。"""
    data: Any = response.data
    if channel == "OFFICIAL_API":
        data = data[0] if isinstance(data, list) and len(data) == 1 else None
    if (
        not isinstance(data, dict)
        or not _remote_identifier(data.get("video_id"))
        or (
            "material_id" in data
            and data["material_id"] is not None
            and not _remote_identifier(data["material_id"])
        )
        or ("advertiser_id" in data and data["advertiser_id"] != advertiser_id)
    ):
        raise RemoteCallError(
            "material_response_unknown", effect="UNKNOWN", evidence=response.evidence
        )
    return material_types.VideoReceipt(
        data["video_id"], data.get("material_id"), response.evidence
    )


def receipt_evidence(receipt: material_types.VideoReceipt) -> dict[str, str]:
    result = {"video_id": receipt.video_id}
    if receipt.mid:
        result["mid"] = receipt.mid
    for key in ("request_id", "mcp_request_id", "remote_task_id"):
        value = getattr(receipt.evidence, key)
        if value:
            result[key] = value
    return result


def video_record_data(record: material_types.VideoRecord) -> dict[str, Any]:
    """旧纯核查规则消费DTO实际字段；不补请求摘要、尺寸或状态。"""
    from dataclasses import asdict

    row = asdict(record)
    row.pop("evidence")
    row["signature"] = row.pop("md5")
    row["material_id"] = row.pop("mid")
    return row


def video_page_data(
    page: material_types.MaterialPage[material_types.VideoRecord],
) -> dict[str, Any]:
    page_info = {
        "page": page.page,
        "page_size": page.page_size,
        "total_page": page.total_pages,
    }
    if page.total_number is not None:
        page_info["total_number"] = page.total_number
    return {
        "list": [video_record_data(row) for row in page.rows],
        "page_info": page_info,
    }
