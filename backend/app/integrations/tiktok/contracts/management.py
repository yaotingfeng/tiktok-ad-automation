"""广告管理写入合同。

该模块只定义冻结命令和外部回执，不负责选择目标或推导权限。写权限必须由
冻结路由和当前账户的搭建权限（及素材开关所需创意授权）在每次物理请求前
重新核验。
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .ads import EntityRef
from .common import CallEvidence

ManagementField = Literal["roas", "budget", "status", "material_status", "name"]
ManagementOutcome = Literal["ACCEPTED", "REJECTED", "NOT_SENT", "UNKNOWN"]
_MANAGEMENT_OPERATIONS = {
    "roas": "update_roas",
    "budget": "update_budget",
    "status": "set_status",
    "material_status": "set_material_status",
    # 改名只针对推广系列；名称写入与预算共用平台的系列更新接口。
    "name": "rename",
}
_ENTITY_KINDS = frozenset(
    {"campaign", "adgroup", "ad", "creative", "material", "account"}
)
_MAX_MATERIAL_BATCH = 20


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > 255:
        raise ValueError(f"invalid {name}")
    return value


def operation_for_field(field: str) -> str | None:
    """字段到管理操作的唯一映射；预览、提交、执行都从这里取，避免多份映射表漂移。"""
    return _MANAGEMENT_OPERATIONS.get(field)


def field_for_operation(operation: str) -> str | None:
    """管理操作到字段的反查；与 operation_for_field 共用同一张映射表。"""
    return next(
        (field for field, name in _MANAGEMENT_OPERATIONS.items() if name == operation),
        None,
    )


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
            status_keys = [
                key for key in ("status", "operation_status") if key in self.desired
            ]
            if len(status_keys) != 1:
                raise ValueError("status requires exactly one status field")
            if self.desired[status_keys[0]] not in {"ENABLE", "DISABLE"}:
                raise ValueError("status must be ENABLE or DISABLE")
        if self.field == "name":
            # 改名命令只携带名称本身，防止借改名夹带其它字段写入。
            if self.ref.kind != "campaign":
                raise ValueError("rename requires campaign identity")
            if set(self.desired) != {"campaign_name"}:
                raise ValueError("rename requires exactly campaign_name")
            # 此处只是结构性守卫（类型、空白、512 字符上限）；完整名称规则（含表情符号）
            # 由预览阶段的 ad_management/rename.py::name_problem 统一执行。
            name = self.desired["campaign_name"]
            if type(name) is not str or not 0 < len(name.strip()) <= 512:
                raise ValueError("invalid campaign name")
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
    reason: str | None = None
    retry_after_ms: int | None = None

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


MANAGEMENT_OPERATIONS = frozenset(_MANAGEMENT_OPERATIONS.values())
MANAGEMENT_ENTITY_KINDS = _ENTITY_KINDS
MAX_MATERIAL_BATCH = _MAX_MATERIAL_BATCH


class ManagementOperations(Protocol):
    def apply(self, command: ManagementCommand) -> ManagementReceipt: ...

    def apply_batch(
        self, commands: tuple[ManagementCommand, ...]
    ) -> tuple[ManagementReceipt, ...]: ...
