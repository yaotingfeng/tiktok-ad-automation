"""创建回执只读取实际 ID/状态；不从请求或成功文案补值。"""

import re

from app.integrations.tiktok.contracts.builds import BuildKind, CreatedObject
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)

ID_KEYS = {
    "CAMPAIGN": "campaign_id",
    "ADGROUP": "adgroup_id",
    "AD": "smart_plus_ad_id",
    "CTA": "creative_portfolio_id",
}


def safe_identifier(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
        else None
    )


def created_result(*, kind: BuildKind, response: McpBusinessResponse) -> CreatedObject:
    data = response.data
    value = data.get(ID_KEYS[kind]) if isinstance(data, dict) else None
    # 回执 ID 会进入内部审计与公开投影；拒绝 URL/控制字符，不保存伪装为 ID 的原文。
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) is None
    ):
        raise RemoteCallError(
            "create_result_unknown", effect="UNKNOWN", evidence=response.evidence
        )
    status = data.get("operation_status") if isinstance(data, dict) else None
    return CreatedObject(
        kind=kind,
        remote_id=value,
        operation_status=safe_identifier(status),
        evidence=response.evidence,
    )


def sdk_creation_envelope(raw: object) -> McpBusinessResponse:
    evidence = (
        CallEvidence(request_id=safe_identifier(raw.get("request_id")))
        if isinstance(raw, dict)
        else CallEvidence()
    )
    if (
        not isinstance(raw, dict)
        or type(raw.get("code")) is not int
        or raw["code"] != 0
        or not isinstance(raw.get("data"), dict)
    ):
        raise RemoteCallError(
            "create_result_unknown", effect="UNKNOWN", evidence=evidence
        )
    return McpBusinessResponse(data=raw["data"], evidence=evidence)
