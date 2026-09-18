from dataclasses import dataclass
from typing import Any, Literal

from app.core.errors import DomainError


@dataclass(frozen=True)
class CallEvidence:
    request_id: str | None = None
    mcp_request_id: str | None = None
    remote_task_id: str | None = None
    # 仅保存通过业务 envelope 校验的数字错误码，不携带可能含凭据的原始文案。
    remote_code: int | None = None


RemoteEffect = Literal["NOT_SENT", "UNKNOWN", "REJECTED_NO_EFFECT"]

# 只有明确未发送的传输故障可重试；权限、参数、契约变化不属于临时网络错误。
TRANSIENT_NOT_SENT = frozenset(
    {
        "mcp_call_failed",
        "mcp_request_failed",
        "mcp_session_open_failed",
        "mcp_session_unavailable",
        # 本地总期限在真实请求发送前耗尽，有持久 NOT_SENT 证据时与连接瞬断
        # 使用同一三次预算；已发送或结果未知仍不会进入此集合。
        "tiktok_call_deadline_exceeded",
    }
)


class RemoteCallError(DomainError):
    def __init__(self, code: str, *, effect: RemoteEffect, evidence: CallEvidence):
        # 上游原文可能包含凭据和签名 URL，异常仅保留应用定义的提示和关联证据。
        super().__init__(code, "TikTok 调用未取得可确认结果", retryable=False)
        self.effect = effect
        self.evidence = evidence


@dataclass(frozen=True)
class McpBusinessResponse:
    """适配器内部回执；业务层须继续转换为对应业务组的类型。"""

    data: dict[str, Any] | list[dict[str, Any]]
    evidence: CallEvidence
