"""按显式工具契约解码业务回执，不从服务消息推断成功或可重发。"""

import json
import math
import re
from typing import Any

from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.mcp.protocol import ToolContract
from mcp.types import CallToolResult, TextContent

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_INVALID = object()
# 官方素材工具实际返回单个 JSON TextContent。这里只规范化响应载体，
# 不改变冻结请求参数/接口版本，也不允许成功文案或任意工具绕过回执校验。
_VIDEO_TEXT_TOOLS = {
    ("materials.upload_video_url", "file_video_ad_upload"),
    ("materials.get_videos", "file_video_ad_info_get"),
    ("materials.search_videos", "file_video_ad_search"),
}


def _identifier(value: Any) -> str | None:
    # 不把浮点 ID 转为字符串；精度一旦丢失便不能作为关联证据。
    if type(value) is int:
        try:
            value = str(value)
        except ValueError:
            return None
    if type(value) is str and _IDENTIFIER.fullmatch(value):
        return value
    return None


def _evidence(raw: Any) -> CallEvidence:
    if type(raw) is not dict:
        return CallEvidence()
    # 不递归搜寻 ID，也不猜测未知 _meta 字段的业务含义。
    return CallEvidence(
        request_id=_identifier(raw.get("request_id")),
        mcp_request_id=_identifier(raw.get("mcp_request_id")),
        remote_task_id=_identifier(raw.get("remote_task_id")),
    )


def _unknown(code: str, evidence: CallEvidence) -> RemoteCallError:
    return RemoteCallError(code, effect="UNKNOWN", evidence=evidence)


def _valid_json(value: Any, depth: int = 0) -> bool:
    # SDK 的 structured_content 为 Any；同样拒绝非 JSON 对象、非有限数和循环结构。
    if depth > 100:
        return False
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_valid_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _valid_json(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _parse_json(text: str) -> Any:
    # 不提取代码块或文字中的片段；json.loads 必须消费完整文本。
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError, RecursionError:
        # 在捕获块外抛业务异常，避免异常链携带原始 JSON 或签名 URL。
        return _INVALID
    return value if _valid_json(value) else _INVALID


def _require_success(raw: Any, evidence: CallEvidence, contract: ToolContract) -> None:
    if (
        type(raw) is not dict
        or not _valid_json(raw)
        or type(raw.get("code")) is not int
    ):
        raise _unknown("mcp_response_invalid", evidence)
    if raw["code"] != 0:
        # 非零 code 本身不能证明没有产生副作用，后续只能由原 attempt 组织核查。
        raise _unknown("mcp_business_error", evidence)
    data = raw.get("data")
    if contract.response_shape == "OBJECT":
        valid_data = type(data) is dict
        if (contract.operation, contract.tool_name) == (
            "materials.upload_video_url",
            "file_video_ad_upload",
        ):
            # 视频上传的官方 API 数据可为单条数组；多条结果不能认作本次文件。
            valid_data = valid_data or (
                type(data) is list and len(data) == 1 and type(data[0]) is dict
            )
    elif contract.response_shape == "OBJECT_LIST":
        valid_data = type(data) is list and all(type(item) is dict for item in data)
    else:
        valid_data = False
    if not valid_data:
        raise _unknown("mcp_response_invalid", evidence)


def _same_json(first: Any, second: Any) -> bool:
    # Python 的相等运算会把 True、1、1.0 混同；比较 JSON 表达以保留类型差异。
    try:
        return json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(
            second, sort_keys=True, allow_nan=False
        )
    except TypeError, ValueError, RecursionError:
        return False


def decode_mcp_result(
    result: CallToolResult, *, contract: ToolContract
) -> McpBusinessResponse:
    raw = result.structured_content
    text_json_allowed = (
        contract.text_json_envelope
        or (contract.operation, contract.tool_name) in _VIDEO_TEXT_TOOLS
    )
    evidence = _evidence(raw)
    text_receipts = []
    for block in result.content:
        if not isinstance(block, TextContent):
            continue
        text = block.text.strip()
        if not text.startswith(("{", "[")):
            continue
        parsed = _parse_json(text)
        if parsed is _INVALID:
            raise _unknown("mcp_response_invalid", evidence)
        text_receipts.append(parsed)

    if len(text_receipts) > 1:
        raise _unknown("mcp_response_ambiguous", evidence)

    if raw is None and text_json_allowed and len(text_receipts) == 1:
        evidence = _evidence(text_receipts[0])
    if result.is_error or result.result_type != "complete":
        raise _unknown("mcp_tool_error", evidence)

    if raw is not None:
        _require_success(raw, evidence, contract)
        # 即使禁止文本回退，出现互相矛盾的双份回执也不能确认业务成功。
        if text_receipts and not _same_json(raw, text_receipts[0]):
            raise _unknown("mcp_response_ambiguous", evidence)
    else:
        if not text_json_allowed or len(text_receipts) != 1:
            raise _unknown("mcp_response_invalid", evidence)
        raw = text_receipts[0]
        evidence = _evidence(raw)
        _require_success(raw, evidence, contract)

    return McpBusinessResponse(data=raw["data"], evidence=evidence)
