"""广告管理写入合同。

该模块只定义冻结命令和外部回执，不负责选择目标或推导权限。写权限必须由
冻结路由和当前账户的 ManagementCapability 在每次物理请求前重新核验。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from .ads import EntityRef
from .common import CallEvidence

ManagementField = Literal["roas", "budget", "status", "material_status"]
ManagementOutcome = Literal["ACCEPTED", "REJECTED", "NOT_SENT", "UNKNOWN"]
_MANAGEMENT_OPERATIONS = {
    "roas": "update_roas",
    "budget": "update_budget",
    "status": "set_status",
    "material_status": "set_material_status",
}
_ENTITY_KINDS = frozenset({"campaign", "adgroup", "ad", "creative", "material", "account"})
_MAX_MATERIAL_BATCH = 50


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 255:
        raise ValueError(f"invalid {name}")
    return value


def _aware(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at needs timezone")


@dataclass(frozen=True)
class ManagementCommand:
    """最小冻结写入命令；字典内容由预览阶段从已验证目录复制。"""

    ref: EntityRef
    field: ManagementField
    original: dict[str, Any]
    desired: dict[str, Any]
    ad_material_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ref, EntityRef):
            raise ValueError("management command requires entity identity")
        if self.field not in _MANAGEMENT_OPERATIONS:
            raise ValueError("unsupported management field")
        if type(self.original) is not dict or type(self.desired) is not dict:
            raise ValueError("management values must be objects")
        # 复制容器，避免调用方在请求构造后改变冻结值。
        object.__setattr__(self, "original", dict(self.original))
        object.__setattr__(self, "desired", dict(self.desired))
        if self.ad_material_id is not None:
            _text(self.ad_material_id, "ad material id")
        if self.field in {"status", "material_status"}:
            status_keys = [key for key in ("status", "operation_status") if key in self.desired]
            if len(status_keys) != 1:
                raise ValueError("status requires exactly one status field")
            if self.desired[status_keys[0]] not in {"ENABLE", "DISABLE"}:
                raise ValueError("status must be ENABLE or DISABLE")
        if self.field == "material_status":
            if self.ref.kind != "ad":
                raise ValueError("material status requires ad identity")
            if self.ad_material_id is None:
                raise ValueError("material status requires ad material reference")
            batch = self.desired.get("ad_material_ids")
            if batch is not None and (
                type(batch) is not list
                or not batch
                or len(batch) > MAX_MATERIAL_BATCH
                or any(type(value) is not str or not value.strip() for value in batch)
            ):
                raise ValueError("material batch exceeds verified platform limit")
        if self.field == "status" and self.ref.kind == "creative":
            raise ValueError("creative status is unsupported")

    @property
    def operation(self) -> str:
        return _MANAGEMENT_OPERATIONS[self.field]

    @property
    def entity_kind(self) -> str:
        return self.ref.kind


@dataclass(frozen=True)
class ManagementReceipt:
    outcome: ManagementOutcome
    request_id: str | None
    retryable: bool
    evidence: CallEvidence | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"ACCEPTED", "REJECTED", "NOT_SENT", "UNKNOWN"}:
            raise ValueError("invalid management outcome")
        if self.request_id is not None:
            _text(self.request_id, "request id")
        if type(self.retryable) is not bool:
            raise ValueError("invalid retryability")
        if self.outcome == "ACCEPTED" and self.retryable:
            raise ValueError("accepted management receipt cannot be retryable")
        if self.evidence is not None and not isinstance(self.evidence, CallEvidence):
            raise ValueError("invalid management evidence")


@dataclass(frozen=True)
class ManagementPermissionEvidence:
    """只记录脱敏授权摘要；scope/工具存在不能单独证明账户管理写权。"""

    scope: tuple[str, ...]
    role: str | None
    operations: frozenset[str]
    source: str
    observed_at: datetime
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.scope) is not tuple or len(set(self.scope)) != len(self.scope):
            raise ValueError("invalid management scopes")
        for value in self.scope + self.tool_names:
            _text(value, "management evidence value")
        if self.role is not None and self.role not in {"ADMIN", "OPERATOR", "ANALYST", "STANDARD"}:
            raise ValueError("invalid management role")
        if type(self.operations) is not frozenset:
            raise ValueError("invalid management operations")
        allowed = frozenset(_MANAGEMENT_OPERATIONS.values())
        if not self.operations <= allowed:
            raise ValueError("unknown management operation")
        _text(self.source, "management evidence source")
        _aware(self.observed_at)


MANAGEMENT_OPERATIONS = frozenset(_MANAGEMENT_OPERATIONS.values())
MANAGEMENT_ENTITY_KINDS = _ENTITY_KINDS
MAX_MATERIAL_BATCH = _MAX_MATERIAL_BATCH


class ManagementOperations(Protocol):
    def apply(self, command: ManagementCommand) -> ManagementReceipt: ...
