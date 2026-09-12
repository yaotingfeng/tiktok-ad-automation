"""封面纯约束与实际回执核实；HTTP统一由SDK/MCP adapter执行。

Signed preview URLs are ephemeral inputs, never persisted as image identities.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import DomainError
from app.integrations.tiktok.contracts import materials as material_types
from app.integrations.tiktok.contracts.common import McpBusinessResponse
from app.modules.materials.sdk_assets import INFO_ENDPOINT as VIDEO_INFO_ENDPOINT

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


def _error() -> DomainError:
    return DomainError("cover_schema_unsupported", "目标素材封面信息无法核实")


def _identifier(value: object) -> str | None:
    from .sdk_assets import _remote_identifier

    return (
        value
        if (
            _remote_identifier(value, limit=255)
            and isinstance(value, str)
            and not value.startswith("//")
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
        )
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


def require_cover_scopes(scope_ids: frozenset[int] | None, *, endpoint: str) -> None:
    # 只检查工厂已经严格解释的不可变scope集合；这里不读取或解密凭据。
    if (
        type(scope_ids) is not frozenset
        or any(type(scope) is not int for scope in scope_ids)
        or endpoint not in _SCOPE_PARENTS
        or not scope_ids & _SCOPE_PARENTS[endpoint]
    ):
        raise DomainError(
            "cover_permission_unverified", "当前连接尚无已核实的封面读写权限"
        )


def validate_image_upload(request: material_types.URLImageUpload) -> None:
    from .sdk_assets import _remote_identifier

    if (
        not _url(request.url)
        or not _remote_identifier(request.advertiser_id)
        or not _remote_identifier(request.file_name, limit=100)
    ):
        raise DomainError("cover_request_invalid", "封面上传参数无效")


def image_receipt(
    response: McpBusinessResponse, *, advertiser_id: str
) -> material_types.ImageReceipt:
    from app.integrations.tiktok.contracts.common import RemoteCallError

    data = response.data
    if (
        type(data) is not dict
        or _identifier(data.get("image_id")) is None
        or ("advertiser_id" in data and data["advertiser_id"] != advertiser_id)
    ):
        raise RemoteCallError(
            "cover_schema_unsupported", effect="UNKNOWN", evidence=response.evidence
        )
    # 可选签名缺失或畸形不能丢弃已经收到的真实image_id；可用性另经详情核实。
    return material_types.ImageReceipt(
        data["image_id"], _signature(data.get("signature")), response.evidence
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
        _identifier(row.get("image_id")) is None
        or row.get("image_id") != image_id
        # TikTok 可按相同图片摘要复用 ID 并更新名称；已有回执摘要时不以名称定归属。
        or (signature is None and row.get("file_name") != remote_name)
        # 官方图片上传成功示例包含 displayable=false；视频封面不等同独立图片广告。
        # 此处核实已返回 ID、内容摘要和视频比例；最终使用仍由广告创建/回读确认。
        or type(row.get("displayable")) is not bool
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


def image_record_data(record: material_types.ImageRecord) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(record)
    data.pop("evidence")
    return data


def image_page_data(
    page: material_types.MaterialPage[material_types.ImageRecord],
) -> dict[str, Any]:
    return {
        "list": [image_record_data(row) for row in page.rows],
        "page_info": {
            "page": page.page,
            "page_size": page.page_size,
            "total_page": page.total_pages,
            "total_number": page.total_number,
        },
    }
