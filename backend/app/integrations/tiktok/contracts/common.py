from dataclasses import dataclass
from typing import Any, Literal

from app.core.errors import DomainError


@dataclass(frozen=True)
class CallEvidence:
    request_id: str | None = None
    mcp_request_id: str | None = None
    remote_task_id: str | None = None


RemoteEffect = Literal["NOT_SENT", "UNKNOWN", "REJECTED_NO_EFFECT"]


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
