"""广告管理阶段 C 的通道无关输入/输出合同。

这些 DTO 在生成预览时冻结数值和影响范围。百分比只在 ``MutationSpec``
被解释一次，任务重试消费保存的最终值，避免重复计算带来的漂移。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.integrations.tiktok.contracts.ads import EntityRef, MaterialUseRef
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

MutationField = Literal["roas", "budget", "status"]
MutationMode = Literal["set", "add", "subtract", "increase_percent", "decrease_percent"]
StatusValue = Literal["ENABLE", "DISABLE"]


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("management values must be finite decimals")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("management values must be finite decimals") from exc
    if not result.is_finite():
        raise ValueError("management values must be finite decimals")
    return result


class MutationSpec(BaseModel):
    """一项批量管理动作；父级和排除集合都是显式输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: MutationField
    mode: MutationMode
    value: Decimal | StatusValue
    include_parents: tuple[EntityRef, ...] = ()
    excluded_refs: tuple[EntityRef, ...] = ()
    excluded_material_uses: tuple[MaterialUseRef, ...] = ()

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: Any) -> Decimal | str:
        if value in ("ENABLE", "DISABLE"):
            return value
        return _decimal(value)

    @model_validator(mode="after")
    def validate_operation(self) -> MutationSpec:
        if self.field == "status":
            if self.mode != "set" or self.value not in {"ENABLE", "DISABLE"}:
                raise ValueError("status only supports set ENABLE or DISABLE")
        elif isinstance(self.value, str):
            raise ValueError("numeric fields require a decimal value")
        elif self.mode == "set" and self.value <= 0:
            raise ValueError("numeric values must be positive")
        elif (
            self.mode in {"add", "subtract", "increase_percent", "decrease_percent"}
            and self.value <= 0
        ):
            raise ValueError("mutation delta must be positive")
        if len(set(self.include_parents)) != len(self.include_parents):
            raise ValueError("duplicate included parent")
        if len(set(self.excluded_refs)) != len(self.excluded_refs):
            raise ValueError("duplicate excluded reference")
        if len(set(self.excluded_material_uses)) != len(self.excluded_material_uses):
            raise ValueError("duplicate excluded material usage")
        return self


class ManagementCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected: int = Field(default=0, ge=0)
    targets: int = Field(default=0, ge=0)
    linked: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)


class ManagementItemPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: EntityRef
    material_use: MaterialUseRef | None = None
    original_value: Decimal | None = None
    final_value: Decimal | str | None = None
    reason: str | None = None
    execution_result: Literal[
        "PENDING",
        "ACCEPTED",
        "REJECTED",
        "NOT_SENT",
        "UNKNOWN",
        "NO_CHANGE",
        "CONFLICT",
        "UNSUPPORTED",
        "CANCELLED",
    ] = "PENDING"
    observation_state: str | None = None
    delivery_status: str | None = None
    request_attribution: str | None = None


class ManagementPreviewPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preview_id: UUID
    digest: str
    created_at: datetime
    expires_at: datetime
    bc_id: str
    route: FrozenTikTokRoute
    items: tuple[ManagementItemPublic, ...] = ()
    counts: ManagementCounts = Field(default_factory=ManagementCounts)


class ManagementTaskPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    bc_id: str
    status: Literal[
        "PREPARING",
        "READY",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "NEEDS_REVIEW",
        "CANCELLED",
    ]
    counts: ManagementCounts = Field(default_factory=ManagementCounts)


class ManagementPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_id: UUID
    bc_id: str = Field(min_length=1, max_length=128)
    mutation: MutationSpec


class ManagementTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: UUID
    preview_digest: str = Field(min_length=64, max_length=64)
    idempotency_key: UUID


class ManagementRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
